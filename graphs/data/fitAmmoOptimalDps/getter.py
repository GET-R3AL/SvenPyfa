# ===============================================================================
# getter.py - Optimal Ammo Selection with Tracking
# ===============================================================================
# 
# This module provides tracking-aware optimal ammo selection for turrets.
# All calculations are based on VOLLEY - DPS is derived by dividing by cycle time.
#
# Key insight: DPS = volley / cycle_time. Since cycle_time is a turret property
# (not affected by ammo), we only need to calculate volley and convert at the end.
#
# ===============================================================================

import math
from bisect import bisect_right
from eos.calc import calculateRangeFactor
from eos.const import FittingHardpoint
from logbook import Logger
from graphs.data.base.getter import SmoothPointGetter
from graphs.data.fitDamageStats.calc.projected import getTackledSpeed, getSigRadiusMult, getScramRange, getScrammables
from service.settings import GraphSettings

pyfalog = Logger(__name__)

# ===============================================================================
# Constants
# ===============================================================================

# Navy faction ammo prefixes (for S/M/L ammo)
NAVY_PREFIXES = (
    'Imperial Navy ',
    'Republic Fleet ', 
    'Caldari Navy ',
    'Federation Navy '
)

# Capital (XL) "navy-tier" faction ammo prefixes
# There is no empire Navy XL ammo, so pirate faction serves as the "navy" tier for capitals
CAPITAL_NAVY_PREFIXES = (
    'Sansha ',
    'Arch Angel ',
    'Shadow '
)

# Projectile damage bands - map prefixes to damage type groups
# Used to filter redundant ammo when target resists are known
PROJECTILE_DAMAGE_BANDS = {
    # EMP variants - EM heavy
    'EMP': 'emp',
    # Phased Plasma variants - Thermal heavy
    'Phased Plasma': 'plasma',
    # Fusion variants - Explosive heavy
    'Fusion': 'fusion',
    # Titanium Sabot / Depleted Uranium - Kinetic heavy (long range)
    'Titanium Sabot': 'kinetic_long',
    'Depleted Uranium': 'kinetic_long',
    # Proton / Nuclear - short range variants
    'Proton': 'proton',
    'Nuclear': 'nuclear',
}


# ===============================================================================
# Utility Functions
# ===============================================================================

def volley_to_dps(volley, cycle_time_ms):
    """
    Convert volley to DPS.
    
    Args:
        volley: Damage per shot
        cycle_time_ms: Cycle time in milliseconds
    
    Returns:
        DPS (damage per second)
    """
    if cycle_time_ms <= 0:
        return 0
    return volley / (cycle_time_ms / 1000)


def filter_projectile_by_band(charges, tgt_resists):
    """
    Filter projectile ammo to only keep the best variant per damage band.
    
    When target resists are known, we only need one variant per damage profile
    since Navy variants just have slightly higher raw damage but identical profiles.
    """
    if not tgt_resists:
        return charges
    
    bands = {}
    other = []
    
    for charge in charges:
        name = charge.name
        band = None
        
        # Strip Navy prefix if present
        base_name = name
        for prefix in NAVY_PREFIXES:
            if name.startswith(prefix):
                base_name = name[len(prefix):]
                break
        
        # Check which band this charge belongs to
        for band_prefix, band_name in PROJECTILE_DAMAGE_BANDS.items():
            if base_name.startswith(band_prefix):
                band = band_name
                break
        
        if band:
            if band not in bands:
                bands[band] = charge
            # Keep non-Navy over Navy (shorter name = base variant)
            elif len(name) < len(bands[band].name):
                bands[band] = charge
        else:
            other.append(charge)
    
    return list(bands.values()) + other


def filter_charges_by_quality(charges, quality_tier):
    """
    Filter charges based on quality tier selection.
    
    Args:
        charges: List of charge items
        quality_tier: 't1', 'navy', or 'all'
    
    Returns:
        Filtered list of charges
        
    Tiers are cumulative:
        - 't1': Tech I (metaGroup 1) + Tech II (metaGroup 2)
        - 'navy': t1 + Navy faction ammo (Imperial Navy, Republic Fleet, Caldari Navy, Federation Navy)
                  For XL (capital) ammo: includes pirate faction (Sansha, Arch Angel, Shadow)
        - 'all': Everything including high-tier faction (Blood, Dark Blood, True Sansha, etc.)
    
    Tech II ammo is always included as it's a distinct ammo type, not a "better" variant.
    """
    if quality_tier == 'all':
        return charges
    
    filtered = []
    for charge in charges:
        mg = charge.metaGroup
        mg_id = mg.ID if mg else None
        
        # Tech I (metaGroup 1) - always included
        if mg_id == 1:
            filtered.append(charge)
            continue
        
        # Tech II (metaGroup 2) - always included (distinct ammo type like Conflagration, Void, etc.)
        if mg_id == 2:
            filtered.append(charge)
            continue
        
        # For 'navy' tier, include Navy faction ammo
        if quality_tier == 'navy' and mg_id == 4:  # Faction
            # Check if it's XL (capital) ammo by name suffix
            is_capital = charge.name.endswith(' XL')
            
            if is_capital:
                # For capital ammo, use pirate faction prefixes as "navy" tier
                if any(charge.name.startswith(prefix) for prefix in CAPITAL_NAVY_PREFIXES):
                    filtered.append(charge)
            else:
                # For subcap ammo, use empire Navy prefixes
                if any(charge.name.startswith(prefix) for prefix in NAVY_PREFIXES):
                    filtered.append(charge)
    
    return filtered if filtered else charges


# ===============================================================================
# EVE Formulas - Turret Tracking
# ===============================================================================

def calcAngularSpeed(atkSpeed, atkAngle, atkRadius, distance, tgtSpeed, tgtAngle, tgtRadius):
    """
    Calculate angular speed (rad/s) between attacker and target.
    
    Angular speed = transversal_velocity / center_to_center_distance
    
    Based on EVE formula from application.py:
    Target is to the right of the attacker, so transversal is projection onto Y axis.
    The relative transversal is the difference of the two transversal components.
    
    Args:
        atkSpeed: Attacker absolute speed (m/s)
        atkAngle: Attacker movement angle (degrees, 0 = towards target)
        atkRadius: Attacker ship radius (m)
        distance: Surface-to-surface distance (m)
        tgtSpeed: Target absolute speed (m/s)
        tgtAngle: Target movement angle (degrees, 0 = towards attacker)
        tgtRadius: Target ship radius (m)
    
    Returns:
        Angular speed in rad/s
    """
    if distance is None:
        return 0
    
    # Convert angles to radians
    atkAngleRad = atkAngle * math.pi / 180
    tgtAngleRad = tgtAngle * math.pi / 180
    
    # Convert to center-to-center distance
    ctcDistance = atkRadius + distance + tgtRadius
    
    # Target is to the right of the attacker, so transversal is projection onto Y axis
    # Relative transversal is the DIFFERENCE (not sum) of transversal components
    transSpeed = abs(atkSpeed * math.sin(atkAngleRad) - tgtSpeed * math.sin(tgtAngleRad))
    
    if ctcDistance == 0:
        return 0 if transSpeed == 0 else math.inf
    else:
        return transSpeed / ctcDistance


def calcTrackingFactor(tracking, optimalSigRadius, angularSpeed, tgtSigRadius):
    """
    Calculate the tracking factor component of chance to hit.
    
    Formula: trackingFactor = 0.5 ^ ((angularSpeed * optimalSigRadius) / (tracking * tgtSigRadius))^2
    
    Args:
        tracking: Turret tracking speed (rad/s)
        optimalSigRadius: Turret's optimal signature radius (m)
        angularSpeed: Angular velocity of target (rad/s)
        tgtSigRadius: Target's signature radius (m)
    
    Returns:
        Tracking factor (0-1)
    """
    if tracking <= 0 or tgtSigRadius <= 0:
        return 0
    if angularSpeed <= 0:
        return 1.0
    
    exponent = (angularSpeed * optimalSigRadius) / (tracking * tgtSigRadius)
    return 0.5 ** (exponent ** 2)


def calcTurretDamageMult(chanceToHit):
    """
    Calculate turret damage multiplier from chance to hit.
    
    Based on EVE formula from application.py:
    https://wiki.eveuniversity.org/Turret_mechanics#Damage
    
    Includes wrecking hit calculation for proper damage distribution.
    
    Note: This can return values > 1.0 at high CTH because wrecking hits
    deal 3x damage. At CTH=1.0, expected damage mult is ~1.015.
    """
    # Wrecking hits: 1% of hits that land do 3x damage
    wreckingChance = min(chanceToHit, 0.01)
    wreckingPart = wreckingChance * 3
    
    # Normal hits: damage varies from (0.01 + 0.49) to (CTH + 0.49)
    normalChance = chanceToHit - wreckingChance
    if normalChance > 0:
        # Average damage multiplier: (min_quality + max_quality) / 2 + 0.49
        # where min_quality = 0.01, max_quality = chanceToHit
        avgDamageMult = (0.01 + chanceToHit) / 2 + 0.49
        normalPart = normalChance * avgDamageMult
    else:
        normalPart = 0
    
    totalMult = normalPart + wreckingPart
    return totalMult


# ===============================================================================
# Turret/Charge Data Extraction
# ===============================================================================

def get_turret_base_stats(mod):
    """
    Get turret stats with ship/skill bonuses but WITHOUT charge modifiers.
    
    When a charge is loaded, getModifiedItemAttr returns values that already
    include the charge's range/falloff/tracking multipliers. We need to undo
    those effects to get the true base turret stats (with only ship/skill bonuses).
    
    Returns dict with: optimal, falloff, tracking, optimalSigRadius, damageMultiplier
    """
    # Get the modified values (includes charge effects if charge is loaded)
    optimal = mod.getModifiedItemAttr('maxRange') or 0
    falloff = mod.getModifiedItemAttr('falloff') or 0
    tracking = mod.getModifiedItemAttr('trackingSpeed') or 0
    optimal_sig_radius = mod.getModifiedItemAttr('optimalSigRadius') or 0
    damage_mult = mod.getModifiedItemAttr('damageMultiplier') or 1
    
    # If a charge is loaded, undo its range/falloff/tracking multiplier effects
    # Charges multiply these stats, so we divide them out to get base stats
    if mod.charge:
        charge_range_mult = mod.charge.getAttribute('weaponRangeMultiplier') or 1
        charge_falloff_mult = mod.charge.getAttribute('fallofMultiplier') or 1  # EVE typo
        charge_tracking_mult = mod.charge.getAttribute('trackingSpeedMultiplier') or 1
        
        if charge_range_mult != 0:
            optimal = optimal / charge_range_mult
        if charge_falloff_mult != 0:
            falloff = falloff / charge_falloff_mult
        if charge_tracking_mult != 0:
            tracking = tracking / charge_tracking_mult
    
    return {
        'optimal': optimal,
        'falloff': falloff,
        'tracking': tracking,
        'optimalSigRadius': optimal_sig_radius,
        'damageMultiplier': damage_mult
    }


def get_charge_stats(charge):
    """
    Extract charge stats including tracking multiplier.
    
    Returns dict with damage values and multipliers.
    """
    em = charge.getAttribute('emDamage') or 0
    thermal = charge.getAttribute('thermalDamage') or 0
    kinetic = charge.getAttribute('kineticDamage') or 0
    explosive = charge.getAttribute('explosiveDamage') or 0
    
    return {
        'emDamage': em,
        'thermalDamage': thermal,
        'kineticDamage': kinetic,
        'explosiveDamage': explosive,
        'totalDamage': em + thermal + kinetic + explosive,
        'rangeMultiplier': charge.getAttribute('weaponRangeMultiplier') or 1,
        'falloffMultiplier': charge.getAttribute('fallofMultiplier') or 1,  # EVE typo
        'trackingMultiplier': charge.getAttribute('trackingSpeedMultiplier') or 1
    }


def apply_resists(charge_stats, tgt_resists):
    """Apply target resists to charge stats."""
    if not tgt_resists:
        return charge_stats
    
    em_res, therm_res, kin_res, explo_res = tgt_resists
    
    em = charge_stats['emDamage'] * (1 - em_res)
    thermal = charge_stats['thermalDamage'] * (1 - therm_res)
    kinetic = charge_stats['kineticDamage'] * (1 - kin_res)
    explosive = charge_stats['explosiveDamage'] * (1 - explo_res)
    
    result = charge_stats.copy()
    result.update({
        'emDamage': em,
        'thermalDamage': thermal,
        'kineticDamage': kinetic,
        'explosiveDamage': explosive,
        'totalDamage': em + thermal + kinetic + explosive
    })
    return result


def get_skill_multiplier(mod):
    """
    Get the skill-based damage multiplier for a turret.
    
    Compares damage with current charge vs base charge damage.
    """
    charge = mod.charge
    if not charge:
        return 1.0
    
    base_damage = (
        (charge.getAttribute('emDamage') or 0) +
        (charge.getAttribute('thermalDamage') or 0) +
        (charge.getAttribute('kineticDamage') or 0) +
        (charge.getAttribute('explosiveDamage') or 0)
    )
    
    if base_damage <= 0:
        return 1.0
    
    modified_damage = (
        (mod.getModifiedChargeAttr('emDamage') or 0) +
        (mod.getModifiedChargeAttr('thermalDamage') or 0) +
        (mod.getModifiedChargeAttr('kineticDamage') or 0) +
        (mod.getModifiedChargeAttr('explosiveDamage') or 0)
    )
    
    return modified_damage / base_damage if base_damage > 0 else 1.0


# ===============================================================================
# Charge Data Precomputation
# ===============================================================================

def precompute_charge_data(turret_base, charges, skill_mult=1.0, tgt_resists=None):
    """
    Pre-compute constant values for each charge.
    
    Returns list of dicts with: name, raw_volley, effective_optimal, 
    effective_falloff, effective_tracking
    
    Note: We do NOT store raw_dps - it's derived from raw_volley / cycle_time
    when needed at the mixin level.
    """
    charge_data = []
    
    for charge in charges:
        stats = get_charge_stats(charge)
        
        # Apply resists early for efficiency
        if tgt_resists:
            stats = apply_resists(stats, tgt_resists)
        
        # Compute effective turret stats with charge modifiers
        effective_optimal = turret_base['optimal'] * stats['rangeMultiplier']
        effective_falloff = turret_base['falloff'] * stats['falloffMultiplier']
        effective_tracking = turret_base['tracking'] * stats['trackingMultiplier']
        
        # Compute raw volley (unmodified by range/tracking)
        raw_volley = stats['totalDamage'] * skill_mult * turret_base['damageMultiplier']
        
        charge_data.append({
            'name': charge.name,
            'raw_volley': raw_volley,
            'effective_optimal': effective_optimal,
            'effective_falloff': effective_falloff,
            'effective_tracking': effective_tracking
        })
    
    return charge_data


# ===============================================================================
# Pareto Filtering (Dominance Pruning)
# ===============================================================================

def is_dominated(a, b):
    """
    Check if charge A is dominated by charge B.
    
    A is dominated if B is >= in ALL dimensions and > in at least one.
    """
    at_least_as_good = (
        b['raw_volley'] >= a['raw_volley'] and
        b['effective_optimal'] >= a['effective_optimal'] and
        b['effective_falloff'] >= a['effective_falloff'] and
        b['effective_tracking'] >= a['effective_tracking']
    )
    
    if not at_least_as_good:
        return False
    
    strictly_better = (
        b['raw_volley'] > a['raw_volley'] or
        b['effective_optimal'] > a['effective_optimal'] or
        b['effective_falloff'] > a['effective_falloff'] or
        b['effective_tracking'] > a['effective_tracking']
    )
    
    return strictly_better


def pareto_filter(charge_data):
    """
    Filter to only non-dominated charges (Pareto frontier).
    
    These are the only charges that could be optimal for some scenario.
    """
    if len(charge_data) <= 1:
        return charge_data
    
    non_dominated = []
    for i, a in enumerate(charge_data):
        dominated = False
        for j, b in enumerate(charge_data):
            if i != j and is_dominated(a, b):
                dominated = True
                break
        if not dominated:
            non_dominated.append(a)
    
    return non_dominated


# ===============================================================================
# Applied Volley Calculation
# ===============================================================================

def calculate_applied_volley(cd, distance, turret_base, tracking_params):
    """
    Calculate applied volley for a charge at a distance.
    
    Applies both range factor and tracking factor.
    
    Args:
        cd: Charge data dict from precompute_charge_data
        distance: Surface-to-surface distance (m)
        turret_base: Base turret stats
        tracking_params: Dict with atkSpeed, atkAngle, atkRadius, tgtSpeed, 
                        tgtAngle, tgtRadius, tgtSigRadius. None = perfect tracking.
    
    Returns:
        Applied volley (damage per shot accounting for range and tracking)
    """
    # Range factor
    if distance <= cd['effective_optimal']:
        range_factor = 1.0
    else:
        range_factor = calculateRangeFactor(
            cd['effective_optimal'],
            cd['effective_falloff'],
            distance,
            restrictedRange=False
        )
    
    # Tracking factor
    if tracking_params is None:
        tracking_factor = 1.0
    else:
        angular_speed = calcAngularSpeed(
            tracking_params['atkSpeed'],
            tracking_params['atkAngle'],
            tracking_params['atkRadius'],
            distance,
            tracking_params['tgtSpeed'],
            tracking_params['tgtAngle'],
            tracking_params['tgtRadius']
        )
        tracking_factor = calcTrackingFactor(
            cd['effective_tracking'],
            turret_base['optimalSigRadius'],
            angular_speed,
            tracking_params['tgtSigRadius']
        )
    
    # Chance to hit and damage multiplier
    cth = range_factor * tracking_factor
    damage_mult = calcTurretDamageMult(cth)
    
    return cd['raw_volley'] * damage_mult


def find_best_charge(charge_data, distance, turret_base, tracking_params):
    """
    Find the best charge at a distance based on applied volley.
    
    Returns: (best_volley, best_name, best_index)
    """
    best_volley = 0
    best_name = None
    best_index = 0
    
    for i, cd in enumerate(charge_data):
        volley = calculate_applied_volley(cd, distance, turret_base, tracking_params)
        if volley > best_volley:
            best_volley = volley
            best_name = cd['name']
            best_index = i
    
    return best_volley, best_name, best_index


# ===============================================================================
# Projected Effects
# ===============================================================================

def get_tracking_params_at_distance(base_params, distance, src, tgt, commonData):
    """
    Get tracking params at a distance with projected effects applied.
    
    Applies webs (reduce speed) and target painters (increase sig).
    """
    if base_params is None:
        return None
    
    params = base_params.copy()
    
    if commonData.get('applyProjected'):
        # Apply webs
        params['tgtSpeed'] = getTackledSpeed(
            src=src,
            tgt=tgt,
            currentUntackledSpeed=params['tgtSpeed'],
            srcScramRange=commonData.get('srcScramRange'),
            tgtScrammables=commonData.get('tgtScrammables', ()),
            webMods=commonData.get('webMods', ()),
            webDrones=commonData.get('webDrones', ()),
            webFighters=commonData.get('webFighters', ()),
            distance=distance
        )
        
        # Apply target painters
        params['tgtSigRadius'] = params['tgtSigRadius'] * getSigRadiusMult(
            src=src,
            tgt=tgt,
            tgtSpeed=params['tgtSpeed'],
            srcScramRange=commonData.get('srcScramRange'),
            tgtScrammables=commonData.get('tgtScrammables', ()),
            tpMods=commonData.get('tpMods', ()),
            tpDrones=commonData.get('tpDrones', ()),
            tpFighters=commonData.get('tpFighters', ()),
            distance=distance
        )
    
    return params


# ===============================================================================
# Transition Points
# ===============================================================================

def calculate_transitions(charge_data, turret_base, base_tracking_params,
                          src, tgt, commonData,
                          max_distance=300000, resolution=1000):
    """
    Calculate distances where optimal ammo changes.
    
    Uses coarse resolution (500m default) for scanning, then binary search
    for exact transition points. This is much faster than fine-grained scanning.
    
    Returns list of tuples: [(distance, charge_index, charge_name, volley), ...]
    """
    if not charge_data:
        pyfalog.debug("[AMMO] calculate_transitions: no charge_data")
        return []
    
    pyfalog.debug(f"[AMMO] Starting transition calculation with {len(charge_data)} charges, resolution={resolution}m, max={max_distance/1000:.0f}km")
    if base_tracking_params:
        pyfalog.debug(f"[AMMO] Base params: tgtSpeed={base_tracking_params.get('tgtSpeed', 0):.0f}, tgtSig={base_tracking_params.get('tgtSigRadius', 0):.0f}")
    
    transitions = []
    current_charge = None
    
    # Start at distance 0
    params_0 = get_tracking_params_at_distance(base_tracking_params, 0, src, tgt, commonData)
    best_volley, best_name, best_idx = find_best_charge(charge_data, 0, turret_base, params_0)
    transitions.append((0, best_idx, best_name, best_volley))
    current_charge = best_name
    
    # Scan for transitions
    distance = resolution
    while distance <= max_distance:
        params = get_tracking_params_at_distance(base_tracking_params, distance, src, tgt, commonData)
        best_volley, best_name, best_idx = find_best_charge(charge_data, distance, turret_base, params)
        
        if best_name != current_charge:
            # Binary search for exact transition point
            low, high = distance - resolution, distance
            while high - low > 10:
                mid = (low + high) // 2
                params_mid = get_tracking_params_at_distance(base_tracking_params, mid, src, tgt, commonData)
                _, mid_name, _ = find_best_charge(charge_data, mid, turret_base, params_mid)
                if mid_name == current_charge:
                    low = mid
                else:
                    high = mid
            
            # Get volley at transition
            params_high = get_tracking_params_at_distance(base_tracking_params, high, src, tgt, commonData)
            best_volley, _, _ = find_best_charge(charge_data, high, turret_base, params_high)
            
            transitions.append((high, best_idx, best_name, best_volley))
            pyfalog.debug(f"[AMMO] Transition @ {high/1000:.1f}km: {current_charge} -> {best_name}")
            current_charge = best_name
        
        distance += resolution
    
    pyfalog.debug(f"[AMMO] Completed: {len(transitions)} transition points found")
    for t in transitions:
        pyfalog.debug(f"[AMMO]   {t[0]/1000:.1f}km: {t[2]}")
    
    return transitions


# ===============================================================================
# Query Functions
# ===============================================================================

def get_volley_at_distance(transitions, charge_data, turret_base, distance,
                           base_tracking_params, src, tgt, commonData):
    """
    Get applied volley at a specific distance.
    
    Uses transitions for O(log n) charge lookup, then calculates exact volley.
    
    Returns: (volley, charge_name)
    """
    if not transitions:
        return 0, None
    
    # Find which charge is optimal at this distance
    distances = [t[0] for t in transitions]
    idx = bisect_right(distances, distance) - 1
    if idx < 0:
        idx = 0
    
    charge_idx = transitions[idx][1]
    cd = charge_data[charge_idx]
    
    # Calculate exact volley with projected effects
    params = get_tracking_params_at_distance(base_tracking_params, distance, src, tgt, commonData)
    volley = calculate_applied_volley(cd, distance, turret_base, params)
    
    return volley, cd['name']


def get_dps_at_distance(transitions, charge_data, turret_base, distance,
                        base_tracking_params, src, tgt, commonData, cycle_time_ms):
    """
    Get applied DPS at a specific distance.
    
    This is simply volley / cycle_time.
    
    Returns: (dps, charge_name)
    """
    volley, name = get_volley_at_distance(
        transitions, charge_data, turret_base, distance,
        base_tracking_params, src, tgt, commonData
    )
    dps = volley_to_dps(volley, cycle_time_ms)
    return dps, name


# ===============================================================================
# Cache Building
# ===============================================================================

def build_turret_cache_entry(mod, quality_tier, tgt_resists, base_tracking_params,
                              src, tgt, commonData, charge_cache=None):
    """
    Build a complete cache entry for a single turret type.
    
    This is the main entry point for building turret data. It:
    1. Gets turret base stats
    2. Gets cycle time
    3. Filters and precomputes charge data
    4. Calculates transition points
    
    Args:
        mod: The turret module
        quality_tier: 't1', 'navy', or 'all'
        tgt_resists: Target resists tuple or None
        base_tracking_params: Base tracking params dict
        src: Source fit wrapper
        tgt: Target wrapper
        commonData: Common data dict with projected effect info
        charge_cache: Optional cache dict for getValidCharges results
    
    Returns:
        Dict with charge_data, transitions, turret_base, cycle_time_ms
        Or None if turret has no valid charges
    """
    pyfalog.debug(f"[AMMO] build_turret_cache_entry START for {mod.item.name}")
    
    # Get turret base stats
    turret_base = get_turret_base_stats(mod)
    
    # Get cycle time
    cycle_params = mod.getCycleParameters()
    if cycle_params is None:
        return None
    cycle_time_ms = cycle_params.averageTime
    
    # Get and filter charges - use cache if available
    charge_cache_key = (mod.item.ID, quality_tier)
    if charge_cache is not None and charge_cache_key in charge_cache:
        charges = charge_cache[charge_cache_key]
        pyfalog.debug(f"[AMMO] Using cached charges: {len(charges)} charges (quality_tier={quality_tier})")
    else:
        all_charges = list(mod.getValidCharges())
        pyfalog.debug(f"[AMMO] Found {len(all_charges)} valid charges, filtering with quality_tier={quality_tier}")
        charges = filter_charges_by_quality(all_charges, quality_tier)
        pyfalog.debug(f"[AMMO] After quality filter: {len(charges)} charges")
        if charge_cache is not None:
            charge_cache[charge_cache_key] = charges
    if not charges:
        return None
    
    # Filter projectile ammo by damage band if using resists
    if tgt_resists:
        charges = filter_projectile_by_band(charges, tgt_resists)
        pyfalog.debug(f"[AMMO] After projectile band filter: {len(charges)} charges")
    
    # Get skill multiplier
    skill_mult = get_skill_multiplier(mod)
    
    # Precompute charge data
    charge_data = precompute_charge_data(turret_base, charges, skill_mult, tgt_resists)
    pyfalog.debug(f"[AMMO] Precomputed {len(charge_data)} charge data entries")
    
    # Calculate transitions
    transitions = calculate_transitions(
        charge_data, turret_base, base_tracking_params,
        src, tgt, commonData
    )
    
    pyfalog.debug(f"[AMMO] build_turret_cache_entry END for {mod.item.name}")
    
    return {
        'charge_data': charge_data,
        'transitions': transitions,
        'turret_base': turret_base,
        'cycle_time_ms': cycle_time_ms,
        'count': 1
    }


def get_ammo_name_at_distance(transitions, distance):
    """
    Fast lookup of ammo name at a distance using pre-computed transitions.
    
    Uses bisect for O(log n) lookup.
    
    Returns:
        Ammo name (str) or None if no transitions
    """
    if not transitions:
        return None
    
    distances = [t[0] for t in transitions]
    idx = bisect_right(distances, distance) - 1
    if idx < 0:
        idx = 0
    
    return transitions[idx][2]


# Alias for backward compatibility
get_ammo_name_at_distance_fast = get_ammo_name_at_distance


# ===============================================================================
# Mixin Classes
# ===============================================================================

class YOptimalAmmoDpsMixin:
    """Y-axis mixin: Calculate DPS using optimal ammo selection."""

    def _getOptimalDpsAtDistance(self, distance, turret_cache, tracking_params, src, tgt, commonData):
        """Get total DPS with optimal ammo at a specific distance."""
        total_dps = 0
        
        for group_info in turret_cache.values():
            volley, _ = get_volley_at_distance(
                group_info['transitions'],
                group_info['charge_data'],
                group_info['turret_base'],
                distance,
                tracking_params,
                src, tgt, commonData
            )
            dps = volley_to_dps(volley, group_info['cycle_time_ms'])
            total_dps += dps * group_info['count']
        
        return total_dps

    def _getOptimalDpsWithAmmoAtDistance(self, distance, turret_cache, tracking_params, src, tgt, commonData):
        """Get total DPS and ammo name at a specific distance."""
        total_dps = 0
        ammo_name = None
        
        for group_info in turret_cache.values():
            volley, name = get_volley_at_distance(
                group_info['transitions'],
                group_info['charge_data'],
                group_info['turret_base'],
                distance,
                tracking_params,
                src, tgt, commonData
            )
            dps = volley_to_dps(volley, group_info['cycle_time_ms'])
            total_dps += dps * group_info['count']
            if ammo_name is None:
                ammo_name = name
        
        return total_dps, ammo_name


class YOptimalAmmoVolleyMixin:
    """Y-axis mixin: Calculate volley using optimal ammo selection."""

    def _getOptimalVolleyAtDistance(self, distance, turret_cache, tracking_params, src, tgt, commonData):
        """Get total volley with optimal ammo at a specific distance."""
        total_volley = 0
        
        for group_info in turret_cache.values():
            volley, _ = get_volley_at_distance(
                group_info['transitions'],
                group_info['charge_data'],
                group_info['turret_base'],
                distance,
                tracking_params,
                src, tgt, commonData
            )
            total_volley += volley * group_info['count']
        
        return total_volley

    def _getOptimalVolleyWithAmmoAtDistance(self, distance, turret_cache, tracking_params, src, tgt, commonData):
        """Get total volley and ammo name at a specific distance."""
        total_volley = 0
        ammo_name = None
        
        for group_info in turret_cache.values():
            volley, name = get_volley_at_distance(
                group_info['transitions'],
                group_info['charge_data'],
                group_info['turret_base'],
                distance,
                tracking_params,
                src, tgt, commonData
            )
            total_volley += volley * group_info['count']
            if ammo_name is None:
                ammo_name = name
        
        return total_volley, ammo_name


class XDistanceMixin(SmoothPointGetter):
    """X-axis mixin: Distance in meters. Builds turret cache and handles lookups."""

    # Coarse resolution for graph display - 500m intervals
    # Exact calculations are done on-demand via getPoint/getPointExtended
    _baseResolution = 100  # ~500m intervals for a 25km range
    _extraDepth = 0  # No extra subdivision - keep it fast

    def _getCommonData(self, miscParams, src, tgt):
        """Build common data including turret cache."""
        # Get settings
        quality_tier = getattr(self.graph, '_ammoQuality', 'all')
        ignore_resists = GraphSettings.getInstance().get('ammoOptimalIgnoreResists')
        applyProjected = GraphSettings.getInstance().get('ammoOptimalApplyProjected')
        
        tgt_resists = None if (ignore_resists or tgt is None) else tgt.getResists()
        tgtSpeed = miscParams.get('tgtSpeed', 0) or 0
        tgtSigRadius = tgt.getSigRadius() if tgt else 0
        
        # Cache key
        cache_key = (id(src.item), quality_tier, tgt_resists, applyProjected, tgtSpeed, tgtSigRadius)
        
        # Initialize graph caches if needed
        if not hasattr(self.graph, '_ammo_turret_cache'):
            self.graph._ammo_turret_cache = {}
        if not hasattr(self.graph, '_ammo_charge_cache'):
            self.graph._ammo_charge_cache = {}
        
        # Build commonData
        commonData = {
            'applyProjected': applyProjected,
            'src_radius': src.getRadius(),
        }
        
        # Add projected effect data if enabled
        if applyProjected:
            commonData['srcScramRange'] = getScramRange(src=src)
            commonData['tgtScrammables'] = getScrammables(tgt=tgt) if tgt else ()
            webMods, tpMods = self.graph._projectedCache.getProjModData(src)
            webDrones, tpDrones = self.graph._projectedCache.getProjDroneData(src)
            webFighters, tpFighters = self.graph._projectedCache.getProjFighterData(src)
            commonData['webMods'] = webMods
            commonData['tpMods'] = tpMods
            commonData['webDrones'] = webDrones
            commonData['tpDrones'] = tpDrones
            commonData['webFighters'] = webFighters
            commonData['tpFighters'] = tpFighters
        
        # Return cached data if available
        if cache_key in self.graph._ammo_turret_cache:
            pyfalog.debug("[AMMO] _getCommonData: CACHE HIT")
            commonData['turret_cache'] = self.graph._ammo_turret_cache[cache_key]
            return commonData
        
        pyfalog.debug(f"[AMMO] _getCommonData: CACHE MISS - building turret cache for {src.item.name}")
        
        # Build base tracking params
        atkSpeed = miscParams.get('atkSpeed', 0) or 0
        atkAngle = miscParams.get('atkAngle', 0) or 0
        tgtAngle = miscParams.get('tgtAngle', 0) or 0
        
        base_tracking_params = {
            'atkSpeed': atkSpeed,
            'atkAngle': atkAngle,
            'atkRadius': src.getRadius(),
            'tgtSpeed': tgtSpeed,
            'tgtAngle': tgtAngle,
            'tgtRadius': tgt.getRadius() if tgt else 0,
            'tgtSigRadius': tgtSigRadius
        }
        
        # Build turret cache
        turret_cache = {}
        for mod in src.item.activeModulesIter():
            if mod.hardpoint != FittingHardpoint.TURRET:
                continue
            if mod.getModifiedItemAttr('miningAmount'):
                continue
            
            key = mod.item.ID
            if key not in turret_cache:
                entry = build_turret_cache_entry(
                    mod, quality_tier, tgt_resists, base_tracking_params,
                    src, tgt, commonData, self.graph._ammo_charge_cache
                )
                if entry:
                    turret_cache[key] = entry
            else:
                turret_cache[key]['count'] += 1
        
        pyfalog.debug(f"[AMMO] _getCommonData: Built cache with {len(turret_cache)} turret groups")
        
        # Cache and return
        self.graph._ammo_turret_cache[cache_key] = turret_cache
        commonData['turret_cache'] = turret_cache
        return commonData

    def _buildTrackingParams(self, distance, miscParams, src, tgt, commonData):
        """Build tracking params at a specific distance with projected effects."""
        tgtSpeed = miscParams.get('tgtSpeed', 0) or 0
        tgtSigRadius = tgt.getSigRadius() if tgt else 0
        
        if tgtSigRadius == 0:
            return None
        
        base_params = {
            'atkSpeed': miscParams.get('atkSpeed', 0) or 0,
            'atkAngle': miscParams.get('atkAngle', 0) or 0,
            'atkRadius': commonData.get('src_radius', 0),
            'tgtSpeed': tgtSpeed,
            'tgtAngle': miscParams.get('tgtAngle', 0) or 0,
            'tgtRadius': tgt.getRadius() if tgt else 0,
            'tgtSigRadius': tgtSigRadius
        }
        
        return get_tracking_params_at_distance(base_params, distance, src, tgt, commonData)

    def _calculatePoint(self, x, miscParams, src, tgt, commonData):
        """Calculate value at distance x."""
        turret_cache = commonData.get('turret_cache', {})
        if not turret_cache:
            return 0
        
        tracking_params = self._buildTrackingParams(x, miscParams, src, tgt, commonData)
        
        if hasattr(self, '_getOptimalDpsAtDistance'):
            return self._getOptimalDpsAtDistance(x, turret_cache, tracking_params, src, tgt, commonData)
        elif hasattr(self, '_getOptimalVolleyAtDistance'):
            return self._getOptimalVolleyAtDistance(x, turret_cache, tracking_params, src, tgt, commonData)
        return 0

    def _calculatePointExtended(self, x, miscParams, src, tgt, commonData):
        """Calculate value and ammo name at distance x."""
        turret_cache = commonData.get('turret_cache', {})
        if not turret_cache:
            return 0, None
        
        tracking_params = self._buildTrackingParams(x, miscParams, src, tgt, commonData)
        
        if hasattr(self, '_getOptimalDpsWithAmmoAtDistance'):
            return self._getOptimalDpsWithAmmoAtDistance(x, turret_cache, tracking_params, src, tgt, commonData)
        elif hasattr(self, '_getOptimalVolleyWithAmmoAtDistance'):
            return self._getOptimalVolleyWithAmmoAtDistance(x, turret_cache, tracking_params, src, tgt, commonData)
        return 0, None


# ===============================================================================
# Getter Classes
# ===============================================================================

class Distance2OptimalAmmoDpsGetter(XDistanceMixin, YOptimalAmmoDpsMixin):
    """Distance vs Optimal Ammo DPS graph getter."""
    
    def getPointExtended(self, x, miscParams, src, tgt):
        commonData = self._getCommonData(miscParams=miscParams, src=src, tgt=tgt)
        value, ammo = self._calculatePointExtended(x, miscParams, src, tgt, commonData)
        return value, {'ammo': ammo}
    
    def getSegments(self, xRange, miscParams, src, tgt):
        """Get plot segments with ammo transition information."""
        commonData = self._getCommonData(miscParams=miscParams, src=src, tgt=tgt)
        turret_cache = commonData.get('turret_cache', {})
        
        if not turret_cache:
            return []
        
        # Get transitions from first turret group
        transitions = None
        for group_info in turret_cache.values():
            transitions = group_info['transitions']
            break
        
        if not transitions:
            return []
        
        # Filter valid transitions (with ammo name)
        valid_transitions = [t for t in transitions if t[2] is not None]
        if not valid_transitions:
            return []
        
        # Build ammo index mapping
        ammo_to_index = {}
        for i, t in enumerate(valid_transitions):
            if t[2] not in ammo_to_index:
                ammo_to_index[t[2]] = len(ammo_to_index)
        
        # Generate segments
        segments = []
        min_x, max_x = xRange
        
        for i, transition in enumerate(valid_transitions):
            trans_dist, _, ammo_name, _ = transition
            seg_start = max(trans_dist, min_x)
            
            # Find segment end
            if i + 1 < len(valid_transitions):
                seg_end = min(valid_transitions[i + 1][0], max_x)
            else:
                seg_end = max_x
            
            if seg_start >= seg_end:
                continue
            
            # Generate points at fixed 500m resolution for performance
            # Exact values are calculated on-demand via getPointExtended
            step = 500  # 500m resolution
            xs, ys = [], []
            x = seg_start
            while x <= seg_end:
                y = self._calculatePoint(x, miscParams, src, tgt, commonData)
                xs.append(x)
                ys.append(y)
                x += step
            
            # Always include the segment end point for smooth transitions
            if xs[-1] < seg_end:
                y = self._calculatePoint(seg_end, miscParams, src, tgt, commonData)
                xs.append(seg_end)
                ys.append(y)
            
            segments.append({
                'xs': xs,
                'ys': ys,
                'ammo': ammo_name,
                'ammoIndex': ammo_to_index[ammo_name]
            })
        
        return segments


class Distance2OptimalAmmoVolleyGetter(XDistanceMixin, YOptimalAmmoVolleyMixin):
    """Distance vs Optimal Ammo Volley graph getter."""
    
    def getPointExtended(self, x, miscParams, src, tgt):
        commonData = self._getCommonData(miscParams=miscParams, src=src, tgt=tgt)
        value, ammo = self._calculatePointExtended(x, miscParams, src, tgt, commonData)
        return value, {'ammo': ammo}
    
    def getSegments(self, xRange, miscParams, src, tgt):
        """Get plot segments with ammo transition information."""
        commonData = self._getCommonData(miscParams=miscParams, src=src, tgt=tgt)
        turret_cache = commonData.get('turret_cache', {})
        
        if not turret_cache:
            return []
        
        # Get transitions from first turret group
        transitions = None
        for group_info in turret_cache.values():
            transitions = group_info['transitions']
            break
        
        if not transitions:
            return []
        
        # Filter valid transitions
        valid_transitions = [t for t in transitions if t[2] is not None]
        if not valid_transitions:
            return []
        
        # Build ammo index mapping
        ammo_to_index = {}
        for i, t in enumerate(valid_transitions):
            if t[2] not in ammo_to_index:
                ammo_to_index[t[2]] = len(ammo_to_index)
        
        # Generate segments
        segments = []
        min_x, max_x = xRange
        
        for i, transition in enumerate(valid_transitions):
            trans_dist, _, ammo_name, _ = transition
            seg_start = max(trans_dist, min_x)
            
            if i + 1 < len(valid_transitions):
                seg_end = min(valid_transitions[i + 1][0], max_x)
            else:
                seg_end = max_x
            
            if seg_start >= seg_end:
                continue
            
            # Generate points at fixed 500m resolution for performance
            # Exact values are calculated on-demand via getPointExtended
            step = 500  # 500m resolution
            xs, ys = [], []
            x = seg_start
            while x <= seg_end:
                y = self._calculatePoint(x, miscParams, src, tgt, commonData)
                xs.append(x)
                ys.append(y)
                x += step
            
            # Always include the segment end point for smooth transitions
            if xs[-1] < seg_end:
                y = self._calculatePoint(seg_end, miscParams, src, tgt, commonData)
                xs.append(seg_end)
                ys.append(y)
            
            segments.append({
                'xs': xs,
                'ys': ys,
                'ammo': ammo_name,
                'ammoIndex': ammo_to_index[ammo_name]
            })
        
        return segments


# ===============================================================================
# Temporary stub for missiles (to be implemented later)
# ===============================================================================

def get_missile_flight_multipliers_from_module(module):
    """Stub - returns default multipliers. Missiles not yet implemented."""
    return {'maxVelocity': 1.0, 'explosionDelay': 1.0}

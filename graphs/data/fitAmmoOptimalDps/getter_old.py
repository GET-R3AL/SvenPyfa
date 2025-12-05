import math
import re
from bisect import bisect_right
from functools import lru_cache
from logbook import Logger

from eos.calc import calculateRangeFactor
from eos.const import FittingHardpoint
from graphs.data.base import SmoothPointGetter
from graphs.data.fitDamageStats.calc.projected import getScramRange, getScrammables, getTackledSpeed, getSigRadiusMult
from service.settings import GraphSettings

pyfalog = Logger(__name__)


# Navy faction ammo prefixes (for S/M/L ammo)
NAVY_PREFIXES = (
    'Imperial Navy',
    'Republic Fleet', 
    'Caldari Navy',
    'Federation Navy'
)

# Capital (XL) "navy-tier" faction ammo prefixes
# There is no empire Navy XL ammo, so pirate faction serves as the "navy" tier for capitals
CAPITAL_NAVY_PREFIXES = (
    'Sansha',
    'Arch Angel',
    'Shadow'
)

# Projectile ammo damage bands - ammo within the same band has identical damage type ratios
# When using resists, we only need to test one ammo per band (the highest damage one)
# Key: band name, Value: tuple of base ammo names (without size suffix or faction prefix)
PROJECTILE_DAMAGE_BANDS = {
    'hail': ('Hail',),
    'quake': ('Quake',),
    'emp_plasma_fusion': ('EMP', 'Phased Plasma', 'Fusion'),
    'du_sabot': ('Depleted Uranium', 'Titanium Sabot'),
    'proton_nuclear_carbonized': ('Proton', 'Nuclear', 'Carbonized Lead'),
    'barrage': ('Barrage',),
    'tremor': ('Tremor',),
}

# Reverse lookup: ammo base name -> band name
PROJECTILE_AMMO_TO_BAND = {}
for band_name, ammo_names in PROJECTILE_DAMAGE_BANDS.items():
    for ammo_name in ammo_names:
        PROJECTILE_AMMO_TO_BAND[ammo_name] = band_name


def get_ammo_base_name_for_band(charge_name):
    """
    Extract base ammo name for damage band lookup.
    Removes size suffix and faction prefixes.
    """
    # Remove size suffix
    cleaned = re.sub(r'\s+(S|M|L|XL)$', '', charge_name, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s+Charge$', '', cleaned, flags=re.IGNORECASE)
    
    # Remove faction prefixes
    faction_prefixes = [
        'Republic Fleet ', 'Imperial Navy ', 'Caldari Navy ', 'Federation Navy ',
        'Dread Guristas ', 'True Sansha ', 'Shadow Serpentis ', 'Domination ',
        'Dark Blood ', "Arch Angel ", 'Guristas ', 'Sansha ', 'Serpentis ',
        'Blood ', 'Angel '
    ]
    for prefix in faction_prefixes:
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):]
            break
    
    return cleaned


def filter_projectile_by_band(charges, tgt_resists):
    """
    Filter projectile charges to only include the best ammo per damage band.
    
    When resists are active, ammo within the same damage band will have the same
    damage type ratios, so only the highest effective damage one matters.
    
    Args:
        charges: List of charge items (should be projectile ammo only)
        tgt_resists: Tuple of (em, therm, kin, explo) resist values (0-1 range)
    
    Returns:
        Filtered list of charges with only the best per band
    """
    if not tgt_resists or all(r == 0 for r in tgt_resists):
        # No resists, no filtering needed
        return charges
    
    em_res, therm_res, kin_res, explo_res = tgt_resists
    
    # Group charges by band and calculate effective damage
    bands = {}  # band_name -> [(effective_damage, charge), ...]
    non_banded = []  # Charges not in any band
    
    for charge in charges:
        base_name = get_ammo_base_name_for_band(charge.name)
        band = PROJECTILE_AMMO_TO_BAND.get(base_name)
        
        if band is None:
            # Not a projectile ammo or unknown band - keep it
            non_banded.append(charge)
            continue
        
        # Calculate effective damage with resists
        em = (charge.getAttribute('emDamage') or 0) * (1 - em_res)
        therm = (charge.getAttribute('thermalDamage') or 0) * (1 - therm_res)
        kin = (charge.getAttribute('kineticDamage') or 0) * (1 - kin_res)
        explo = (charge.getAttribute('explosiveDamage') or 0) * (1 - explo_res)
        effective_damage = em + therm + kin + explo
        
        if band not in bands:
            bands[band] = []
        bands[band].append((effective_damage, charge))
    
    # Pick the best charge from each band
    filtered = list(non_banded)
    for band_name, band_charges in bands.items():
        # Sort by effective damage descending, pick the best
        band_charges.sort(key=lambda x: x[0], reverse=True)
        if band_charges:
            filtered.append(band_charges[0][1])
    
    return filtered


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
    
    return filtered


# Turret damage multiplier calculation (from fitDamageStats)
# Accounts for wrecking shots and hit quality distribution
@lru_cache(maxsize=100)
def calcTurretDamageMult(chanceToHit):
    """
    Calculate damage multiplier for turret-based weapons.
    
    This accounts for:
    - Wrecking shots: 1% of hits do 3x damage
    - Normal hits: damage varies from 0.5x to (CTH + 0.49)x, averaged
    
    Source: https://wiki.eveuniversity.org/Turret_mechanics#Damage
    """
    if chanceToHit <= 0:
        return 0
    
    wreckingChance = min(chanceToHit, 0.01)
    wreckingPart = wreckingChance * 3
    normalChance = chanceToHit - wreckingChance
    if normalChance > 0:
        avgDamageMult = (0.01 + chanceToHit) / 2 + 0.49
        normalPart = normalChance * avgDamageMult
    else:
        normalPart = 0
    totalMult = normalPart + wreckingPart
    return totalMult


def calcAngularSpeed(atkSpeed, atkAngle, atkRadius, distance, tgtSpeed, tgtAngle, tgtRadius):
    """
    Calculate angular speed based on mobility parameters of two ships.
    
    Args:
        atkSpeed: Attacker's absolute speed (m/s)
        atkAngle: Attacker's movement angle in degrees (0 = towards target)
        atkRadius: Attacker ship's radius
        distance: Surface-to-surface distance between ships
        tgtSpeed: Target's absolute speed (m/s)  
        tgtAngle: Target's movement angle in degrees (0 = towards attacker)
        tgtRadius: Target ship's radius
    
    Returns:
        Angular speed in radians/second
    """
    if distance is None:
        return 0
    atkAngleRad = atkAngle * math.pi / 180
    tgtAngleRad = tgtAngle * math.pi / 180
    ctcDistance = atkRadius + distance + tgtRadius  # center-to-center
    # Target is to the right of the attacker, so transversal is projection onto Y axis
    transSpeed = abs(atkSpeed * math.sin(atkAngleRad) - tgtSpeed * math.sin(tgtAngleRad))
    if ctcDistance == 0:
        return 0 if transSpeed == 0 else math.inf
    else:
        return transSpeed / ctcDistance


def calcTrackingFactor(atkTracking, atkOptimalSigRadius, angularSpeed, tgtSigRadius):
    """
    Calculate tracking chance to hit component.
    
    Formula: 0.5 ^ (((angularSpeed * optimalSigRadius) / (tracking * targetSig)) ^ 2)
    
    Args:
        atkTracking: Turret's tracking speed (rad/s)
        atkOptimalSigRadius: Turret's optimal signature radius
        angularSpeed: Angular speed between attacker and target (rad/s)
        tgtSigRadius: Target's signature radius
    
    Returns:
        Tracking factor (0-1), where 1 = perfect tracking
    """
    if atkTracking == 0 or tgtSigRadius == 0:
        return 0
    return 0.5 ** (((angularSpeed * atkOptimalSigRadius) / (atkTracking * tgtSigRadius)) ** 2)


def get_turret_base_stats(module):
    """
    Get turret stats with ship/skill bonuses but WITHOUT charge modifiers.
    
    If a charge is loaded, it affects maxRange, falloff, and trackingSpeed via multipliers.
    We need to undo those effects to get the true base turret stats.
    
    Returns dict with: optimal, falloff, tracking, optimalSigRadius, damageMultiplier
    """
    # Get the modified values (includes charge effects if charge is loaded)
    optimal = module.getModifiedItemAttr('maxRange') or 0
    falloff = module.getModifiedItemAttr('falloff') or 0
    tracking = module.getModifiedItemAttr('trackingSpeed') or 0
    optimal_sig_radius = module.getModifiedItemAttr('optimalSigRadius') or 0
    damage_mult = module.getModifiedItemAttr('damageMultiplier') or 1
    
    # If a charge is loaded, undo its range/falloff/tracking multiplier effects
    # Charges multiply these stats, so we divide them out
    if module.charge:
        charge_range_mult = module.charge.getAttribute('weaponRangeMultiplier') or 1
        charge_falloff_mult = module.charge.getAttribute('fallofMultiplier') or 1  # EVE typo
        charge_tracking_mult = module.charge.getAttribute('trackingSpeedMultiplier') or 1
        
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
    Get the stats from a charge item.
    
    Returns dict with damage values and range/falloff multipliers.
    """
    em = charge.getAttribute('emDamage') or 0
    thermal = charge.getAttribute('thermalDamage') or 0
    kinetic = charge.getAttribute('kineticDamage') or 0
    explosive = charge.getAttribute('explosiveDamage') or 0
    total_damage = em + thermal + kinetic + explosive
    
    range_mult = charge.getAttribute('weaponRangeMultiplier') or 1
    falloff_mult = charge.getAttribute('fallofMultiplier') or 1  # Note: typo in EVE data
    
    return {
        'emDamage': em,
        'thermalDamage': thermal,
        'kineticDamage': kinetic,
        'explosiveDamage': explosive,
        'totalDamage': total_damage,
        'rangeMultiplier': range_mult,
        'falloffMultiplier': falloff_mult
    }


def apply_resists_to_charge_stats(charge_stats, tgt_resists):
    """
    Apply target resists to charge stats, returning effective damage values.
    
    This modifies the damage values by multiplying each by (1 - resist).
    
    Args:
        charge_stats: Dict from get_charge_stats()
        tgt_resists: Tuple of (em, therm, kin, explo) resist values (0-1 range)
    
    Returns:
        New dict with resist-adjusted damage values
    """
    if not tgt_resists:
        return charge_stats
    
    em_res, therm_res, kin_res, explo_res = tgt_resists
    
    em = charge_stats['emDamage'] * (1 - em_res)
    thermal = charge_stats['thermalDamage'] * (1 - therm_res)
    kinetic = charge_stats['kineticDamage'] * (1 - kin_res)
    explosive = charge_stats['explosiveDamage'] * (1 - explo_res)
    total_damage = em + thermal + kinetic + explosive
    
    return {
        'emDamage': em,
        'thermalDamage': thermal,
        'kineticDamage': kinetic,
        'explosiveDamage': explosive,
        'totalDamage': total_damage,
        'rangeMultiplier': charge_stats['rangeMultiplier'],
        'falloffMultiplier': charge_stats['falloffMultiplier']
    }


def get_charge_skill_multiplier(module):
    """
    Calculate the skill bonus multiplier for charge damage.
    
    Some ships/skills boost charge damage directly. We calculate the ratio
    between modified (with skills) and raw (without skills) damage to apply
    to all charges.
    
    Returns multiplier (default 1.0 if no charge loaded or no bonus).
    """
    if not module.charge:
        return 1.0
    
    # Get raw damage from charge
    raw_em = module.charge.getAttribute('emDamage') or 0
    raw_therm = module.charge.getAttribute('thermalDamage') or 0
    raw_kin = module.charge.getAttribute('kineticDamage') or 0
    raw_exp = module.charge.getAttribute('explosiveDamage') or 0
    raw_total = raw_em + raw_therm + raw_kin + raw_exp
    
    if raw_total == 0:
        return 1.0
    
    # Get modified damage (with skills)
    mod_em = module.getModifiedChargeAttr('emDamage') or 0
    mod_therm = module.getModifiedChargeAttr('thermalDamage') or 0
    mod_kin = module.getModifiedChargeAttr('kineticDamage') or 0
    mod_exp = module.getModifiedChargeAttr('explosiveDamage') or 0
    mod_total = mod_em + mod_therm + mod_kin + mod_exp
    
    return mod_total / raw_total


# =============================================================================
# TRACKING-AWARE TURRET FUNCTIONS
# =============================================================================
# These functions properly account for tracking when selecting optimal ammo.
# Key insight: Angular speed decreases with distance, so tracking matters more
# at close range and less at long range. Different charges have different
# effective tracking via trackingSpeedMultiplier.
#
# Algorithm:
# 1. Pre-compute charge data INCLUDING effective tracking per charge
# 2. Apply Pareto dominance pruning to eliminate charges that can never be optimal
# 3. For each distance point, calculate applied DPS with full tracking formula
# 4. Cache results keyed by (fit_id, quality, resists, tgtSpeed, tgtSig, ...)
# =============================================================================


def get_charge_stats_with_tracking(charge):
    """
    Get the stats from a charge item, INCLUDING tracking multiplier.
    
    Returns dict with damage values, range/falloff/tracking multipliers.
    """
    em = charge.getAttribute('emDamage') or 0
    thermal = charge.getAttribute('thermalDamage') or 0
    kinetic = charge.getAttribute('kineticDamage') or 0
    explosive = charge.getAttribute('explosiveDamage') or 0
    total_damage = em + thermal + kinetic + explosive
    
    range_mult = charge.getAttribute('weaponRangeMultiplier') or 1
    falloff_mult = charge.getAttribute('fallofMultiplier') or 1  # Note: typo in EVE data
    tracking_mult = charge.getAttribute('trackingSpeedMultiplier') or 1
    
    return {
        'emDamage': em,
        'thermalDamage': thermal,
        'kineticDamage': kinetic,
        'explosiveDamage': explosive,
        'totalDamage': total_damage,
        'rangeMultiplier': range_mult,
        'falloffMultiplier': falloff_mult,
        'trackingMultiplier': tracking_mult
    }


def apply_resists_to_charge_stats_with_tracking(charge_stats, tgt_resists):
    """
    Apply target resists to charge stats (tracking-aware version).
    
    Preserves tracking multiplier in output.
    """
    if not tgt_resists:
        return charge_stats
    
    em_res, therm_res, kin_res, explo_res = tgt_resists
    
    em = charge_stats['emDamage'] * (1 - em_res)
    thermal = charge_stats['thermalDamage'] * (1 - therm_res)
    kinetic = charge_stats['kineticDamage'] * (1 - kin_res)
    explosive = charge_stats['explosiveDamage'] * (1 - explo_res)
    total_damage = em + thermal + kinetic + explosive
    
    return {
        'emDamage': em,
        'thermalDamage': thermal,
        'kineticDamage': kinetic,
        'explosiveDamage': explosive,
        'totalDamage': total_damage,
        'rangeMultiplier': charge_stats['rangeMultiplier'],
        'falloffMultiplier': charge_stats['falloffMultiplier'],
        'trackingMultiplier': charge_stats['trackingMultiplier']
    }


def precompute_charge_data_with_tracking(turret_base, charges, cycle_time_ms, skill_multiplier=1.0, tgt_resists=None):
    """
    Pre-compute constant values for each charge, INCLUDING effective tracking.
    
    Returns list of dicts with: name, raw_dps, raw_volley, effective_optimal, 
    effective_falloff, effective_tracking
    
    Unlike the non-tracking version, this does NOT sort by raw_dps because
    the "best" charge depends on distance AND target parameters.
    
    Args:
        turret_base: Base turret stats (optimal, falloff, tracking, optimalSigRadius, damageMultiplier)
        charges: List of charge items
        cycle_time_ms: Turret cycle time in milliseconds
        skill_multiplier: Multiplier for charge damage from skills (default 1.0)
        tgt_resists: Target resist tuple (em, therm, kin, explo) in 0-1 range, or None to ignore
    """
    charge_data = []
    for charge in charges:
        charge_stats = get_charge_stats_with_tracking(charge)
        
        # Apply target resists EARLY (before dominance pruning) for efficiency
        if tgt_resists:
            charge_stats = apply_resists_to_charge_stats_with_tracking(charge_stats, tgt_resists)
        
        # These are constant regardless of distance
        effective_optimal = turret_base['optimal'] * charge_stats['rangeMultiplier']
        effective_falloff = turret_base['falloff'] * charge_stats['falloffMultiplier']
        effective_tracking = turret_base['tracking'] * charge_stats['trackingMultiplier']
        
        # Apply skill multiplier to charge damage
        adjusted_damage = charge_stats['totalDamage'] * skill_multiplier
        raw_volley = adjusted_damage * turret_base['damageMultiplier']
        raw_dps = raw_volley / (cycle_time_ms / 1000)
        
        charge_data.append({
            'name': charge.name,
            'raw_dps': raw_dps,
            'raw_volley': raw_volley,
            'effective_optimal': effective_optimal,
            'effective_falloff': effective_falloff,
            'effective_tracking': effective_tracking
        })
    
    return charge_data


def is_dominated(charge_a, charge_b):
    """
    Check if charge_a is dominated by charge_b.
    
    A charge is dominated if another charge is at least as good in ALL dimensions
    and strictly better in at least one dimension.
    
    Dimensions for turrets:
    - raw_dps (higher = better)
    - effective_optimal (higher = better for range)
    - effective_falloff (higher = better for range)
    - effective_tracking (higher = better against moving targets)
    
    Returns True if charge_a is dominated by charge_b (meaning charge_a can be pruned).
    """
    # Check if b is at least as good in all dimensions
    at_least_as_good = (
        charge_b['raw_dps'] >= charge_a['raw_dps'] and
        charge_b['effective_optimal'] >= charge_a['effective_optimal'] and
        charge_b['effective_falloff'] >= charge_a['effective_falloff'] and
        charge_b['effective_tracking'] >= charge_a['effective_tracking']
    )
    
    if not at_least_as_good:
        return False
    
    # Check if b is strictly better in at least one dimension
    strictly_better = (
        charge_b['raw_dps'] > charge_a['raw_dps'] or
        charge_b['effective_optimal'] > charge_a['effective_optimal'] or
        charge_b['effective_falloff'] > charge_a['effective_falloff'] or
        charge_b['effective_tracking'] > charge_a['effective_tracking']
    )
    
    return strictly_better


def pareto_filter_charges(charge_data):
    """
    Filter charge data to only include non-dominated charges (Pareto frontier).
    
    A charge on the Pareto frontier is one that is NOT dominated by any other charge.
    These are the only charges that could potentially be optimal for some scenario.
    
    This is an O(n²) operation but typically n is small (10-20 charges) and
    this is done once per cache computation.
    
    Returns list of non-dominated charge dicts.
    """
    if len(charge_data) <= 1:
        return charge_data
    
    non_dominated = []
    dominated_names = []
    
    for i, charge_a in enumerate(charge_data):
        dominated = False
        for j, charge_b in enumerate(charge_data):
            if i == j:
                continue
            if is_dominated(charge_a, charge_b):
                dominated = True
                dominated_names.append(charge_a['name'])
                break
        
        if not dominated:
            non_dominated.append(charge_a)
    
    # Log Pareto filter results
    if dominated_names:
        pyfalog.debug(f"[TRACKING] Pareto filter: {len(charge_data)} -> {len(non_dominated)} charges")
        pyfalog.debug(f"[TRACKING] Dominated (pruned): {dominated_names}")
        pyfalog.debug(f"[TRACKING] Non-dominated: {[c['name'] for c in non_dominated]}")
    
    return non_dominated


def calculate_applied_dps_at_distance(charge_data_item, distance, turret_base, tracking_params):
    """
    Calculate the true applied DPS for a single charge at a specific distance.
    
    This applies BOTH range factor AND tracking factor.
    
    Args:
        charge_data_item: Single charge dict from precompute_charge_data_with_tracking
        distance: Surface-to-surface distance in meters
        turret_base: Base turret stats (for optimalSigRadius)
        tracking_params: Dict with target movement parameters:
            - atkSpeed: Attacker absolute speed (m/s)
            - atkAngle: Attacker movement angle (degrees, 0 = towards target)
            - atkRadius: Attacker ship radius
            - tgtSpeed: Target absolute speed (m/s)
            - tgtAngle: Target movement angle (degrees, 0 = towards attacker)
            - tgtRadius: Target ship radius
            - tgtSigRadius: Target signature radius
            If None, assumes perfect tracking (stationary target).
    
    Returns: applied_dps (float)
    """
    cd = charge_data_item
    
    # Calculate range factor
    if distance <= cd['effective_optimal']:
        range_factor = 1.0
    else:
        range_factor = calculateRangeFactor(
            cd['effective_optimal'], 
            cd['effective_falloff'], 
            distance, 
            restrictedRange=False
        )
    
    # Calculate tracking factor
    if tracking_params is None:
        tracking_factor = 1.0  # Perfect tracking
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
    
    # Chance to hit = range_factor * tracking_factor
    cth = range_factor * tracking_factor
    turret_mult = calcTurretDamageMult(cth)
    applied_dps = cd['raw_dps'] * turret_mult
    
    return applied_dps


def calculate_applied_volley_at_distance(charge_data_item, distance, turret_base, tracking_params):
    """
    Calculate the true applied volley for a single charge at a specific distance.
    
    Same as calculate_applied_dps_at_distance but uses raw_volley instead.
    """
    cd = charge_data_item
    
    # Calculate range factor
    if distance <= cd['effective_optimal']:
        range_factor = 1.0
    else:
        range_factor = calculateRangeFactor(
            cd['effective_optimal'], 
            cd['effective_falloff'], 
            distance, 
            restrictedRange=False
        )
    
    # Calculate tracking factor
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
    
    cth = range_factor * tracking_factor
    turret_mult = calcTurretDamageMult(cth)
    applied_volley = cd['raw_volley'] * turret_mult
    
    return applied_volley


def find_best_charge_at_distance_tracking_aware(charge_data, distance, turret_base, tracking_params):
    """
    Find the best charge at a specific distance, accounting for tracking.
    
    This evaluates ALL non-dominated charges (after Pareto filtering) and
    returns the one with highest applied DPS.
    
    Args:
        charge_data: List of charge dicts (ideally already Pareto-filtered)
        distance: Distance in meters
        turret_base: Base turret stats dict
        tracking_params: Target movement parameters dict, or None for perfect tracking
    
    Returns: (best_dps, best_charge_name, best_index)
    """
    best_dps = 0
    best_charge_name = None
    best_index = 0
    
    # Debug: log all candidates at key distances
    debug_at_distance = (distance == 0 or distance % 10000 == 0)
    candidates = []
    
    for i, cd in enumerate(charge_data):
        applied_dps = calculate_applied_dps_at_distance(cd, distance, turret_base, tracking_params)
        
        if debug_at_distance:
            candidates.append((cd['name'], applied_dps))
        
        if applied_dps > best_dps:
            best_dps = applied_dps
            best_charge_name = cd['name']
            best_index = i
    
    # Log at key distances for debugging
    if debug_at_distance and candidates:
        pyfalog.debug(f"[TRACKING] @ {distance/1000:.0f}km: best={best_charge_name} ({best_dps:.1f} DPS)")
        for name, dps in sorted(candidates, key=lambda x: -x[1])[:5]:  # Top 5
            pyfalog.debug(f"[TRACKING]   - {name}: {dps:.1f} DPS")
    
    return best_dps, best_charge_name, best_index


def calculate_transition_points_tracking_aware(charge_data, turret_base, tracking_params, 
                                                max_distance=300000, resolution=100):
    """
    Calculate the distances where optimal ammo changes, accounting for tracking.
    
    Unlike the non-tracking version, this must evaluate all charges at each point
    because tracking changes the optimal choice based on angular speed (which
    varies with distance).
    
    Args:
        charge_data: List of charge dicts (should be Pareto-filtered for efficiency)
        turret_base: Base turret stats dict
        tracking_params: Target movement parameters dict, or None for perfect tracking
        max_distance: Maximum distance to analyze
        resolution: Distance step for scanning
    
    Returns list of tuples: [(distance, charge_index, charge_name, dps), ...]
    sorted by distance ascending.
    """
    if not charge_data:
        return []
    
    transitions = []
    current_charge = None
    
    # Start at distance 0
    best_dps, best_name, best_idx = find_best_charge_at_distance_tracking_aware(
        charge_data, 0, turret_base, tracking_params
    )
    transitions.append((0, best_idx, best_name, best_dps))
    current_charge = best_name
    
    # Scan through distances to find transitions
    distance = resolution
    while distance <= max_distance:
        best_dps, best_name, best_idx = find_best_charge_at_distance_tracking_aware(
            charge_data, distance, turret_base, tracking_params
        )
        
        if best_name != current_charge:
            # Found a transition - binary search to find exact point
            low = distance - resolution
            high = distance
            while high - low > 10:  # 10m precision
                mid = (low + high) // 2
                _, mid_name, _ = find_best_charge_at_distance_tracking_aware(
                    charge_data, mid, turret_base, tracking_params
                )
                if mid_name == current_charge:
                    low = mid
                else:
                    high = mid
            
            # Recalculate DPS at exact transition point
            best_dps, _, _ = find_best_charge_at_distance_tracking_aware(
                charge_data, high, turret_base, tracking_params
            )
            
            # Record the transition
            transitions.append((high, best_idx, best_name, best_dps))
            current_charge = best_name
        
        # If DPS is effectively zero, stop
        if best_dps < 0.01:
            break
            
        distance += resolution
    
    return transitions


def get_dps_at_distance_tracking_aware(transitions, charge_data, turret_base, distance, tracking_params):
    """
    Get DPS at a specific distance using pre-computed transitions (tracking-aware).
    
    Uses bisect for O(log n) lookup of which charge is optimal, then calculates
    exact DPS at that distance.
    
    Args:
        transitions: List of (distance, charge_index, charge_name, dps) tuples
        charge_data: Pre-computed charge data
        turret_base: Base turret stats dict
        distance: Distance in meters
        tracking_params: Target movement parameters dict, or None for perfect tracking
    
    Returns: (dps, charge_name)
    """
    if not transitions:
        return 0, None
    
    # Find the transition that applies at this distance
    distances = [t[0] for t in transitions]
    idx = bisect_right(distances, distance) - 1
    if idx < 0:
        idx = 0
    
    # Get the charge that's optimal at this distance
    transition = transitions[idx]
    charge_idx = transition[1]
    cd = charge_data[charge_idx]
    
    # Calculate exact DPS at this distance
    applied_dps = calculate_applied_dps_at_distance(cd, distance, turret_base, tracking_params)
    
    return applied_dps, cd['name']


def get_volley_at_distance_tracking_aware(transitions, charge_data, turret_base, distance, tracking_params):
    """
    Get volley at a specific distance using pre-computed transitions (tracking-aware).
    
    Same as get_dps_at_distance_tracking_aware but uses volley instead.
    """
    if not transitions:
        return 0, None
    
    distances = [t[0] for t in transitions]
    idx = bisect_right(distances, distance) - 1
    if idx < 0:
        idx = 0
    
    transition = transitions[idx]
    charge_idx = transition[1]
    cd = charge_data[charge_idx]
    
    applied_volley = calculate_applied_volley_at_distance(cd, distance, turret_base, tracking_params)
    
    return applied_volley, cd['name']


def build_tracking_params(atkSpeed, atkAngle, atkRadius, tgtSpeed, tgtAngle, tgtRadius, tgtSigRadius):
    """
    Build a tracking_params dict from individual parameters.
    
    Convenience function to create the dict expected by tracking-aware functions.
    """
    return {
        'atkSpeed': atkSpeed,
        'atkAngle': atkAngle,
        'atkRadius': atkRadius,
        'tgtSpeed': tgtSpeed,
        'tgtAngle': tgtAngle,
        'tgtRadius': tgtRadius,
        'tgtSigRadius': tgtSigRadius
    }


def get_tracking_params_at_distance(base_tracking_params, distance, src, tgt, commonData):
    """
    Get tracking parameters at a specific distance, accounting for projected effects.
    
    Uses the same projected effect handling as the Damage Stats graph via
    getTackledSpeed() and getSigRadiusMult() from projected.py.
    
    Args:
        base_tracking_params: Base tracking params dict with unmodified values
        distance: Distance in meters
        src: Source fit wrapper (for projected effect calculations)
        tgt: Target wrapper (for projected effect calculations)
        commonData: Dict containing projected effect data from _getCommonData():
            - applyProjected: bool
            - srcScramRange: scram range or None
            - tgtScrammables: list of scrammable modules
            - webMods, webDrones, webFighters: web effect data
            - tpMods, tpDrones, tpFighters: target painter effect data
    
    Returns: Modified tracking_params dict for this distance
    """
    if base_tracking_params is None:
        return None
    
    # Start with base values
    params = base_tracking_params.copy()
    
    # Apply projected effects (webs and target painters) if enabled
    if commonData.get('applyProjected'):
        # Apply webs to reduce target speed
        params['tgtSpeed'] = getTackledSpeed(
            src=src,
            tgt=tgt,
            currentUntackledSpeed=params['tgtSpeed'],
            srcScramRange=commonData.get('srcScramRange'),
            tgtScrammables=commonData.get('tgtScrammables', ()),
            webMods=commonData.get('webMods', ()),
            webDrones=commonData.get('webDrones', ()),
            webFighters=commonData.get('webFighters', ()),
            distance=distance)
        
        # Apply target painters to increase signature radius
        params['tgtSigRadius'] = params['tgtSigRadius'] * getSigRadiusMult(
            src=src,
            tgt=tgt,
            tgtSpeed=params['tgtSpeed'],
            srcScramRange=commonData.get('srcScramRange'),
            tgtScrammables=commonData.get('tgtScrammables', ()),
            tpMods=commonData.get('tpMods', ()),
            tpDrones=commonData.get('tpDrones', ()),
            tpFighters=commonData.get('tpFighters', ()),
            distance=distance)
    
    return params


def calculate_transition_points_with_projected(charge_data, turret_base, base_tracking_params,
                                                src, tgt, commonData,
                                                max_distance=300000, resolution=100):
    """
    Calculate transition points with projected effects applied per-distance.
    
    This is the full tracking-aware version that accounts for:
    - Angular speed varying with distance
    - Webs reducing target speed (range-dependent)
    - Target painters increasing signature (range-dependent)
    
    Since projected effects vary by distance, we can't simply pre-compute transitions
    once and reuse them. This function recalculates at each distance point.
    
    Args:
        charge_data: List of charge dicts (should be Pareto-filtered)
        turret_base: Base turret stats dict
        base_tracking_params: Base tracking params (unmodified tgtSpeed/tgtSigRadius)
        src: Source fit wrapper
        tgt: Target wrapper
        commonData: Dict with projected effect data
        max_distance: Maximum distance to analyze
        resolution: Distance step for scanning
    
    Returns list of tuples: [(distance, charge_index, charge_name, dps), ...]
    """
    if not charge_data:
        pyfalog.debug("[TRACKING] calculate_transition_points_with_projected: no charge_data")
        return []
    
    pyfalog.debug(f"[TRACKING] Starting transition calculation with {len(charge_data)} charges")
    if base_tracking_params:
        pyfalog.debug(f"[TRACKING] Base params: tgtSpeed={base_tracking_params.get('tgtSpeed', 0):.0f}, tgtSig={base_tracking_params.get('tgtSigRadius', 0):.0f}")
    
    transitions = []
    current_charge = None
    
    # Start at distance 0
    tracking_params_0 = get_tracking_params_at_distance(
        base_tracking_params, 0, src, tgt, commonData)
    best_dps, best_name, best_idx = find_best_charge_at_distance_tracking_aware(
        charge_data, 0, turret_base, tracking_params_0
    )
    transitions.append((0, best_idx, best_name, best_dps))
    current_charge = best_name
    
    # Scan through distances to find transitions
    distance = resolution
    while distance <= max_distance:
        # Get tracking params with projected effects at this distance
        tracking_params = get_tracking_params_at_distance(
            base_tracking_params, distance, src, tgt, commonData)
        
        best_dps, best_name, best_idx = find_best_charge_at_distance_tracking_aware(
            charge_data, distance, turret_base, tracking_params
        )
        
        if best_name != current_charge:
            # Found a transition - binary search to find exact point
            low = distance - resolution
            high = distance
            while high - low > 10:  # 10m precision
                mid = (low + high) // 2
                tracking_params_mid = get_tracking_params_at_distance(
                    base_tracking_params, mid, src, tgt, commonData)
                _, mid_name, _ = find_best_charge_at_distance_tracking_aware(
                    charge_data, mid, turret_base, tracking_params_mid
                )
                if mid_name == current_charge:
                    low = mid
                else:
                    high = mid
            
            # Recalculate DPS at exact transition point
            tracking_params_high = get_tracking_params_at_distance(
                base_tracking_params, high, src, tgt, commonData)
            best_dps, _, _ = find_best_charge_at_distance_tracking_aware(
                charge_data, high, turret_base, tracking_params_high
            )
            
            # Record the transition
            transitions.append((high, best_idx, best_name, best_dps))
            pyfalog.debug(f"[TRACKING] Transition @ {high/1000:.1f}km: {current_charge} -> {best_name} ({best_dps:.1f} DPS)")
            current_charge = best_name
        
        # NOTE: We do NOT break early based on low DPS!
        # With angular tracking, DPS can be low at close range (high angular velocity)
        # but improve as distance increases (lower angular velocity = better tracking).
        # The loop continues until max_distance is reached.
            
        distance += resolution
    
    # Log final transition summary
    pyfalog.debug(f"[TRACKING] Completed: {len(transitions)} transition points found")
    for t in transitions:
        pyfalog.debug(f"[TRACKING]   {t[0]/1000:.1f}km: {t[2]} ({t[3]:.1f} DPS)")
    
    return transitions


def get_dps_at_distance_with_projected(transitions, charge_data, turret_base, distance,
                                        base_tracking_params, src, tgt, commonData):
    """
    Get DPS at a specific distance using transitions, with projected effects.
    
    Uses bisect for O(log n) lookup of which charge is optimal, then calculates
    exact DPS at that distance with projected effects applied.
    
    Args:
        transitions: List of (distance, charge_index, charge_name, dps) tuples
        charge_data: Pre-computed charge data
        turret_base: Base turret stats dict
        distance: Distance in meters
        base_tracking_params: Base tracking params (unmodified)
        src: Source fit wrapper
        tgt: Target wrapper
        commonData: Dict with projected effect data
    
    Returns: (dps, charge_name)
    """
    if not transitions:
        return 0, None
    
    # Find the transition that applies at this distance
    distances = [t[0] for t in transitions]
    idx = bisect_right(distances, distance) - 1
    if idx < 0:
        idx = 0
    
    # Get the charge that's optimal at this distance
    transition = transitions[idx]
    charge_idx = transition[1]
    cd = charge_data[charge_idx]
    
    # Get tracking params with projected effects at this specific distance
    tracking_params = get_tracking_params_at_distance(
        base_tracking_params, distance, src, tgt, commonData)
    
    # Calculate exact DPS at this distance
    applied_dps = calculate_applied_dps_at_distance(cd, distance, turret_base, tracking_params)
    
    return applied_dps, cd['name']


def get_volley_at_distance_with_projected(transitions, charge_data, turret_base, distance,
                                           base_tracking_params, src, tgt, commonData):
    """
    Get volley at a specific distance using transitions, with projected effects.
    
    Same as get_dps_at_distance_with_projected but returns volley.
    """
    if not transitions:
        return 0, None
    
    distances = [t[0] for t in transitions]
    idx = bisect_right(distances, distance) - 1
    if idx < 0:
        idx = 0
    
    transition = transitions[idx]
    charge_idx = transition[1]
    cd = charge_data[charge_idx]
    
    tracking_params = get_tracking_params_at_distance(
        base_tracking_params, distance, src, tgt, commonData)
    
    applied_volley = calculate_applied_volley_at_distance(cd, distance, turret_base, tracking_params)
    
    return applied_volley, cd['name']


# =============================================================================
# MISSILE FUNCTIONS
# =============================================================================

@lru_cache(maxsize=200)
def calcMissileApplicationFactor(atkEr, atkEv, atkDrf, tgtSpeed, tgtSigRadius):
    """
    Calculate missile application factor.
    
    Formula: min(1, tgtSigRadius/eR, ((eV * tgtSigRadius) / (eR * tgtSpeed))^DRF)
    
    Args:
        atkEr: Missile explosion radius (aoeCloudSize)
        atkEv: Missile explosion velocity (aoeVelocity)
        atkDrf: Missile damage reduction factor (aoeDamageReductionFactor)
        tgtSpeed: Target speed
        tgtSigRadius: Target signature radius
    
    Returns: Application factor (0-1)
    """
    factors = [1]
    # "Slow" part - signature vs explosion radius
    if atkEr > 0:
        factors.append(tgtSigRadius / atkEr)
    # "Fast" part - explosion velocity vs target speed
    if tgtSpeed > 0 and atkEr > 0:
        factors.append(((atkEv * tgtSigRadius) / (atkEr * tgtSpeed)) ** atkDrf)
    return min(factors)


# Damage type priority for tie-breaking (EM > Thermal > Kinetic > Explosive)
# Lower value = higher priority
DAMAGE_TYPE_PRIORITY = {
    'em': 0,
    'thermal': 1,
    'kinetic': 2,
    'explosive': 3
}


def get_missile_dominant_damage_type(charge_name):
    """
    Determine the dominant damage type of a missile based on its name.
    
    Mjolnir = EM, Inferno = Thermal, Scourge = Kinetic, Nova = Explosive
    
    Returns: 'em', 'thermal', 'kinetic', 'explosive', or 'unknown'
    """
    name_lower = charge_name.lower()
    if 'mjolnir' in name_lower:
        return 'em'
    elif 'inferno' in name_lower:
        return 'thermal'
    elif 'scourge' in name_lower:
        return 'kinetic'
    elif 'nova' in name_lower:
        return 'explosive'
    return 'unknown'


# Functions to extract skill/ship multipliers from the currently-loaded charge.
# Since effects modify chargeModifiedAttributes during fit calculation,
# we can't simply swap charges and get modified stats. Instead, we compute
# the ratio of modified/base values from the current charge and apply to others.

def get_missile_multipliers_with_temp_charge(module, charges, fit):
    """
    Get missile skill/ship multipliers by temporarily loading charges.
    
    Loads one charge of each damage type (Mjolnir=EM, Inferno=Thermal, 
    Scourge=Kinetic, Nova=Explosive) to extract per-damage-type multipliers.
    
    Uses the same missile variant (preferring Rage/Fury > T1 standard) for all
    damage types to ensure fair comparison.
    
    Args:
        module: The launcher module
        charges: List of valid charges (to pick ones to load temporarily)
        fit: The fit object (needed for recalculation)
    
    Returns tuple: (damage_mults, flight_mults, app_mults)
    """
    original_charge = module.charge
    
    # Find one charge of each damage type, preferring same variant
    # Mjolnir = EM, Inferno = Thermal, Scourge = Kinetic, Nova = Explosive
    # Priority: Rage/Fury (highest damage) > T1 standard (baseline)
    damage_type_charges = {
        'em': None,       # Mjolnir
        'thermal': None,  # Inferno
        'kinetic': None,  # Scourge
        'explosive': None # Nova
    }
    
    # First pass: look for Rage/Fury variants (consistent high-damage variant)
    for charge in charges:
        name = charge.name
        is_rage_fury = 'Rage' in name or 'Fury' in name
        if not is_rage_fury:
            continue
        if 'Mjolnir' in name and damage_type_charges['em'] is None:
            damage_type_charges['em'] = charge
        elif 'Inferno' in name and damage_type_charges['thermal'] is None:
            damage_type_charges['thermal'] = charge
        elif 'Scourge' in name and damage_type_charges['kinetic'] is None:
            damage_type_charges['kinetic'] = charge
        elif 'Nova' in name and damage_type_charges['explosive'] is None:
            damage_type_charges['explosive'] = charge
    
    # Second pass: fill gaps with T1 standard (no prefix, no Precision/Javelin)
    for charge in charges:
        name = charge.name
        # Skip faction/advanced variants
        if any(x in name for x in ['Rage', 'Fury', 'Precision', 'Javelin', 'Navy', 'Guristas', 'Sansha', 'Serpentis', 'Blood', 'Angel', 'Domination']):
            continue
        if 'Mjolnir' in name and damage_type_charges['em'] is None:
            damage_type_charges['em'] = charge
        elif 'Inferno' in name and damage_type_charges['thermal'] is None:
            damage_type_charges['thermal'] = charge
        elif 'Scourge' in name and damage_type_charges['kinetic'] is None:
            damage_type_charges['kinetic'] = charge
        elif 'Nova' in name and damage_type_charges['explosive'] is None:
            damage_type_charges['explosive'] = charge
    
    # Third pass: fill any remaining gaps with whatever we can find
    for charge in charges:
        name = charge.name
        if 'Mjolnir' in name and damage_type_charges['em'] is None:
            damage_type_charges['em'] = charge
        elif 'Inferno' in name and damage_type_charges['thermal'] is None:
            damage_type_charges['thermal'] = charge
        elif 'Scourge' in name and damage_type_charges['kinetic'] is None:
            damage_type_charges['kinetic'] = charge
        elif 'Nova' in name and damage_type_charges['explosive'] is None:
            damage_type_charges['explosive'] = charge
    
    pyfalog.error(f"Selected charges for multiplier extraction: em={damage_type_charges['em'].name if damage_type_charges['em'] else None}, th={damage_type_charges['thermal'].name if damage_type_charges['thermal'] else None}, kin={damage_type_charges['kinetic'].name if damage_type_charges['kinetic'] else None}, exp={damage_type_charges['explosive'].name if damage_type_charges['explosive'] else None}")
    
    # Extract multipliers for each damage type
    damage_mults = {
        'emDamage': 1.0,
        'thermalDamage': 1.0,
        'kineticDamage': 1.0,
        'explosiveDamage': 1.0
    }
    flight_mults = None
    app_mults = None
    
    # Map damage types to their attribute names and charge
    type_mapping = [
        ('em', 'emDamage', damage_type_charges['em']),
        ('thermal', 'thermalDamage', damage_type_charges['thermal']),
        ('kinetic', 'kineticDamage', damage_type_charges['kinetic']),
        ('explosive', 'explosiveDamage', damage_type_charges['explosive']),
    ]
    
    for dmg_type, attr_name, charge in type_mapping:
        if charge is None:
            continue
        
        # Load this charge temporarily
        module.charge = charge
        fit.clear()
        fit.calculateModifiedAttributes()
        
        # Extract the multiplier for this damage type
        modified = module.getModifiedChargeAttr(attr_name) or 0
        base = module.getChargeBaseAttrValue(attr_name) or 0
        if base > 0 and modified > 0:
            damage_mults[attr_name] = modified / base
            pyfalog.error(f"  {dmg_type} multiplier from {charge.name}: {damage_mults[attr_name]:.3f}")
        
        # Also extract flight and application mults from first charge we test
        if flight_mults is None:
            flight_mults = get_missile_flight_multipliers_from_module(module)
            app_mults = get_missile_application_multipliers_from_module(module)
    
    # Restore original state
    module.charge = original_charge
    fit.clear()
    fit.calculateModifiedAttributes()
    pyfalog.error(f"Restored original charge state")
    
    # Set defaults if we didn't extract them
    if flight_mults is None:
        flight_mults = {'maxVelocity': 1.0, 'explosionDelay': 1.0}
    if app_mults is None:
        app_mults = {'aoeCloudSize': 1.0, 'aoeVelocity': 1.0, 'aoeDamageReductionFactor': 1.0}
    
    pyfalog.error(f"Final damage multipliers: em={damage_mults['emDamage']:.3f}, th={damage_mults['thermalDamage']:.3f}, kin={damage_mults['kineticDamage']:.3f}, exp={damage_mults['explosiveDamage']:.3f}")
    
    return damage_mults, flight_mults, app_mults


def get_missile_damage_multipliers_from_module(module):
    """
    Calculate per-damage-type multipliers by comparing modified to base damage values.
    
    This captures all skill bonuses (Warhead Upgrades, etc.) and ship bonuses
    that affect missile damage. Different damage types may have different bonuses
    (e.g., Gila has kinetic/thermal bonus plus additional kinetic-only bonus).
    
    Returns dict with multipliers for each damage type.
    """
    if module.charge is None:
        return {
            'emDamage': 1.0,
            'thermalDamage': 1.0,
            'kineticDamage': 1.0,
            'explosiveDamage': 1.0
        }
    
    multipliers = {}
    for dmgType in ('emDamage', 'thermalDamage', 'kineticDamage', 'explosiveDamage'):
        modified = module.getModifiedChargeAttr(dmgType) or 0
        base = module.getChargeBaseAttrValue(dmgType) or 0
        if base > 0 and modified > 0:
            multipliers[dmgType] = modified / base
        else:
            # If this damage type isn't present on the charge, we need to estimate
            # For Scourge (kinetic), we only get kinetic ratio directly
            # Use 1.0 as fallback - will be refined below
            multipliers[dmgType] = 1.0
    
    # Log what we got
    pyfalog.error(f"  Per-type multipliers from {module.charge.name}: em={multipliers['emDamage']:.3f}, th={multipliers['thermalDamage']:.3f}, kin={multipliers['kineticDamage']:.3f}, exp={multipliers['explosiveDamage']:.3f}")
    
    return multipliers


def get_missile_flight_multipliers_from_module(module):
    """
    Calculate flight attribute multipliers by comparing modified to base values.
    
    This captures skill bonuses from Missile Projection, Missile Bombardment,
    and ship bonuses that affect flight time/velocity.
    
    Returns dict with multipliers for maxVelocity and explosionDelay.
    """
    if module.charge is None:
        return {'maxVelocity': 1.0, 'explosionDelay': 1.0}
    
    multipliers = {}
    
    for attr in ('maxVelocity', 'explosionDelay'):
        modified = module.getModifiedChargeAttr(attr) or 0
        base = module.getChargeBaseAttrValue(attr) or 0
        if base > 0 and modified > 0:
            multipliers[attr] = modified / base
        else:
            multipliers[attr] = 1.0
    
    return multipliers


def get_missile_application_multipliers_from_module(module):
    """
    Calculate application attribute multipliers by comparing modified to base values.
    
    This captures skills like Guided Missile Precision, Target Navigation Prediction,
    and rigging/implant bonuses that affect explosion radius/velocity.
    
    Returns dict with multipliers for aoeCloudSize, aoeVelocity, aoeDamageReductionFactor.
    """
    if module.charge is None:
        return {'aoeCloudSize': 1.0, 'aoeVelocity': 1.0, 'aoeDamageReductionFactor': 1.0}
    
    multipliers = {}
    
    for attr in ('aoeCloudSize', 'aoeVelocity', 'aoeDamageReductionFactor'):
        modified = module.getModifiedChargeAttr(attr) or 0
        base = module.getChargeBaseAttrValue(attr) or 0
        if base > 0:
            multipliers[attr] = modified / base if modified > 0 else 1.0
        else:
            multipliers[attr] = 1.0
    
    return multipliers


def precompute_missile_charge_data(module, charges, cycle_time_ms, ship_radius, 
                                    damage_mults=None, flight_mults=None, app_mults=None,
                                    tgt_resists=None):
    """
    Pre-compute constant values for each missile charge.
    
    Takes pre-calculated multipliers from a reference charge (extracted earlier).
    
    Args:
        module: The launcher module
        charges: List of valid charges to test
        cycle_time_ms: Launcher cycle time in milliseconds
        ship_radius: Ship radius for flight calculations
        damage_mults: Dict of per-damage-type multipliers (em, thermal, kinetic, explosive)
        flight_mults: Dict with maxVelocity and explosionDelay multipliers
        app_mults: Dict with aoeCloudSize, aoeVelocity, aoeDamageReductionFactor multipliers
        tgt_resists: Optional target resist tuple (em, therm, kin, explo)
    
    Returns list of dicts sorted by raw_dps DESCENDING, with damage type priority as tie-breaker.
    """
    if damage_mults is None:
        damage_mults = {'emDamage': 1.0, 'thermalDamage': 1.0, 'kineticDamage': 1.0, 'explosiveDamage': 1.0}
    if flight_mults is None:
        flight_mults = {'maxVelocity': 1.0, 'explosionDelay': 1.0}
    if app_mults is None:
        app_mults = {'aoeCloudSize': 1.0, 'aoeVelocity': 1.0, 'aoeDamageReductionFactor': 1.0}
    
    # Get launcher damage multiplier (from module itself, not charge)
    launcher_damage_mult = module.getModifiedItemAttr('damageMultiplier') or 1
    
    charge_data = []
    for charge in charges:
        try:
            # Get BASE charge attributes (without skill bonuses)
            base_em = charge.getAttribute('emDamage') or 0
            base_thermal = charge.getAttribute('thermalDamage') or 0
            base_kinetic = charge.getAttribute('kineticDamage') or 0
            base_explosive = charge.getAttribute('explosiveDamage') or 0
            base_total = base_em + base_thermal + base_kinetic + base_explosive
            
            # Apply per-damage-type multipliers from skills/ship
            em = base_em * damage_mults['emDamage']
            thermal = base_thermal * damage_mults['thermalDamage']
            kinetic = base_kinetic * damage_mults['kineticDamage']
            explosive = base_explosive * damage_mults['explosiveDamage']
            total_damage = em + thermal + kinetic + explosive
            
            # Get base flight attributes
            base_velocity = charge.getAttribute('maxVelocity') or 0
            base_explosion_delay = charge.getAttribute('explosionDelay') or 0
            base_mass = charge.getAttribute('mass') or 1
            base_agility = charge.getAttribute('agility') or 1
            
            if base_velocity <= 0 or base_explosion_delay <= 0:
                continue
            
            # Apply flight multipliers
            maxVelocity = base_velocity * flight_mults['maxVelocity']
            explosionDelay = base_explosion_delay * flight_mults['explosionDelay']
            
            # Calculate range using same formula as module.missileMaxRangeData
            flightTime = explosionDelay / 1000 + ship_radius / maxVelocity
            
            def calculateRange(vel, mass, agility, ft):
                accelTime = min(ft, mass * agility / 1000000)
                duringAcceleration = vel / 2 * accelTime
                fullSpeed = vel * (ft - accelTime)
                return duringAcceleration + fullSpeed
            
            lowerTime = math.floor(flightTime)
            higherTime = math.ceil(flightTime)
            lowerRange = calculateRange(maxVelocity, base_mass, base_agility, lowerTime)
            higherRange = calculateRange(maxVelocity, base_mass, base_agility, higherTime)
            lowerRange = max(0, lowerRange - ship_radius)
            higherRange = max(0, higherRange - ship_radius)
            higherChance = flightTime - lowerTime
            
            # Get base application attributes and apply multipliers
            base_aoeCloudSize = charge.getAttribute('aoeCloudSize') or 0
            base_aoeVelocity = charge.getAttribute('aoeVelocity') or 0
            base_aoeDrf = charge.getAttribute('aoeDamageReductionFactor') or 1
            
            aoeCloudSize = base_aoeCloudSize * app_mults['aoeCloudSize']
            aoeVelocity = base_aoeVelocity * app_mults['aoeVelocity']
            aoeDrf = base_aoeDrf * app_mults['aoeDamageReductionFactor']
            
            pyfalog.error(f"Missile {charge.name}: base_dmg={base_total:.1f} -> {total_damage:.1f}, range={lowerRange:.0f}-{higherRange:.0f}m")
            
            # Apply target resists if provided
            if tgt_resists:
                em_res, therm_res, kin_res, explo_res = tgt_resists
                em = em * (1 - em_res)
                thermal = thermal * (1 - therm_res)
                kinetic = kinetic * (1 - kin_res)
                explosive = explosive * (1 - explo_res)
                total_damage = em + thermal + kinetic + explosive
            
            raw_volley = total_damage * launcher_damage_mult
            raw_dps = raw_volley / (cycle_time_ms / 1000)
            
            # Get damage type priority for tie-breaking
            damage_type = get_missile_dominant_damage_type(charge.name)
            damage_priority = DAMAGE_TYPE_PRIORITY.get(damage_type, 99)
            
            charge_data.append({
                'name': charge.name,
                'raw_dps': raw_dps,
                'raw_volley': raw_volley,
                'lowerRange': lowerRange,
                'higherRange': higherRange,
                'higherChance': higherChance,
                'aoeCloudSize': aoeCloudSize,
                'aoeVelocity': aoeVelocity,
                'aoeDamageReductionFactor': aoeDrf,
                'damage_priority': damage_priority
            })
        except Exception as e:
            pyfalog.error(f"Error processing missile charge {charge.name}: {str(e)}")
            continue
    
    # Sort by raw_dps descending, then by damage priority ascending (EM first) for tie-breaking
    charge_data.sort(key=lambda x: (-x['raw_dps'], x['damage_priority']))
    
    if charge_data:
        pyfalog.error(f"Precomputed {len(charge_data)} missile charges, top: {charge_data[0]['name']} @ {charge_data[0]['raw_dps']:.1f} DPS")
    
    return charge_data


def calculate_missile_best_dps_at_distance(charge_data, distance, tgtSpeed, tgtSigRadius, start_index=0):
    """
    Find the best missile charge at a specific distance.
    
    Uses damage type priority (EM > Thermal > Kinetic > Explosive) as tie-breaker
    when multiple missiles have the same effective DPS.
    
    Args:
        charge_data: List of charge dicts, sorted by raw_dps descending then damage_priority ascending
        distance: Distance in meters
        tgtSpeed: Target speed (m/s)
        tgtSigRadius: Target signature radius
        start_index: Index to start searching from
    
    Returns: (best_dps, best_charge_name, new_start_index)
    """
    best_dps = 0
    best_charge_name = None
    best_index = start_index
    best_priority = 99  # Lower is better
    
    for i in range(start_index, len(charge_data)):
        cd = charge_data[i]
        
        # Calculate range factor
        if distance <= cd['lowerRange']:
            range_factor = 1.0
        elif distance <= cd['higherRange']:
            range_factor = cd['higherChance']
        else:
            range_factor = 0.0
        
        if range_factor == 0:
            continue
        
        # Calculate application factor
        app_factor = calcMissileApplicationFactor(
            cd['aoeCloudSize'],
            cd['aoeVelocity'],
            cd['aoeDamageReductionFactor'],
            tgtSpeed,
            tgtSigRadius
        )
        
        effective_dps = cd['raw_dps'] * range_factor * app_factor
        
        # Tie-break: higher DPS wins; if equal, lower damage_priority wins (EM > Thermal > Kinetic > Explosive)
        if effective_dps > best_dps or (effective_dps == best_dps and cd['damage_priority'] < best_priority):
            best_dps = effective_dps
            best_charge_name = cd['name']
            best_index = i
            best_priority = cd['damage_priority']
    
    return best_dps, best_charge_name, best_index


def calculate_missile_transition_points(charge_data, tgtSpeed, tgtSigRadius, max_distance=300000, resolution=100):
    """
    Calculate the distances where optimal missile ammo changes.
    
    Returns list of tuples: [(distance, charge_index, charge_name, dps), ...]
    """
    if not charge_data:
        return []
    
    transitions = []
    current_index = 0
    current_charge = None
    
    # Start at distance 0
    best_dps, best_name, best_idx = calculate_missile_best_dps_at_distance(
        charge_data, 0, tgtSpeed, tgtSigRadius, 0)
    if best_name:
        transitions.append((0, best_idx, best_name, best_dps))
        current_index = best_idx
        current_charge = best_name
    
    # Scan through distances to find transitions
    distance = resolution
    while distance <= max_distance:
        best_dps, best_name, best_idx = calculate_missile_best_dps_at_distance(
            charge_data, distance, tgtSpeed, tgtSigRadius, 0)
        
        if best_name != current_charge and best_name is not None:
            # Found a transition - binary search to find exact point
            low = distance - resolution
            high = distance
            while high - low > 10:  # 10m precision
                mid = (low + high) // 2
                _, mid_name, _ = calculate_missile_best_dps_at_distance(
                    charge_data, mid, tgtSpeed, tgtSigRadius, 0)
                if mid_name == current_charge:
                    low = mid
                else:
                    high = mid
            
            # Record the transition
            transitions.append((high, best_idx, best_name, best_dps))
            current_index = best_idx
            current_charge = best_name
        
        # If DPS is effectively zero, add a None transition and stop
        if best_dps < 0.01:
            # Add None transition to mark where DPS drops to zero
            transitions.append((distance, -1, None, 0))
            break
            
        distance += resolution
    
    return transitions


def get_missile_dps_at_distance(charge_data, transitions, distance, tgtSpeed, tgtSigRadius):
    """
    Get missile DPS at a specific distance using pre-computed transitions.
    
    Returns: (dps, charge_name)
    """
    if not transitions:
        return 0, None
    
    # Find the transition that applies at this distance
    distances = [t[0] for t in transitions]
    idx = bisect_right(distances, distance) - 1
    if idx < 0:
        idx = 0
    
    # Get the charge that's optimal at this distance
    transition = transitions[idx]
    charge_idx = transition[1]
    
    # Calculate exact DPS at this distance using that charge
    cd = charge_data[charge_idx]
    
    # Calculate range factor
    if distance <= cd['lowerRange']:
        range_factor = 1.0
    elif distance <= cd['higherRange']:
        range_factor = cd['higherChance']
    else:
        range_factor = 0.0
    
    # Calculate application factor
    app_factor = calcMissileApplicationFactor(
        cd['aoeCloudSize'],
        cd['aoeVelocity'],
        cd['aoeDamageReductionFactor'],
        tgtSpeed,
        tgtSigRadius
    )
    
    dps = cd['raw_dps'] * range_factor * app_factor
    
    return dps, cd['name']


class YOptimalAmmoDpsMixin:
    """Calculate DPS using optimal ammo selection for turrets and missiles."""

    def _getOptimalDpsAtDistance(self, src, distance, turret_cache=None, missile_cache=None, tracking_base=None,
                                    tgt=None, commonData=None):
        """
        Get total DPS with optimal ammo selection at a specific distance.
        Uses pre-computed transition points for O(log n) lookup.
        
        Args:
            src: Source fit wrapper
            distance: Distance in meters
            turret_cache: Pre-computed turret data (charge_data, transitions, tracking stats)
            missile_cache: Pre-computed missile data (charge_data, transitions, application stats)
            tracking_base: Base tracking params dict (without turret-specific stats), or None for perfect tracking
            tgt: Target wrapper (for tracking-aware mode)
            commonData: Common data dict (for tracking-aware mode)
        """
        total_dps = 0
        
        # Process turrets
        if turret_cache:
            for group_key, group_info in turret_cache.items():
                transitions = group_info['transitions']
                charge_data = group_info['charge_data']
                count = group_info['count']
                
                # Tracking-aware ammo selection
                # Transitions were computed with tracking awareness
                # We recalculate DPS at the exact query distance
                turret_base = group_info.get('turret_base')
                if turret_base and tracking_base and commonData:
                    dps, _ = get_dps_at_distance_with_projected(
                        transitions, charge_data, turret_base, distance,
                        tracking_base, src, tgt, commonData)
                else:
                    # Fallback: use simple tracking-aware lookup
                    dps, _ = get_dps_at_distance_tracking_aware(
                        transitions, charge_data, turret_base, distance, tracking_base)
                
                total_dps += dps * count
        
        # Process missiles
        if missile_cache:
            tgtSpeed = tracking_base.get('tgtSpeed', 0) if tracking_base else 0
            tgtSigRadius = tracking_base.get('tgtSigRadius', 0) if tracking_base else 0
            
            for group_key, group_info in missile_cache.items():
                transitions = group_info['transitions']
                charge_data = group_info['charge_data']
                count = group_info['count']
                
                dps, ammo = get_missile_dps_at_distance(charge_data, transitions, distance, tgtSpeed, tgtSigRadius)
                total_dps += dps * count
        
        return total_dps

    def _getOptimalDpsWithAmmoAtDistance(self, src, distance, turret_cache=None, missile_cache=None, tracking_base=None,
                                          tgt=None, commonData=None):
        """
        Get total DPS and optimal ammo name at a specific distance.
        Returns (total_dps, ammo_name) tuple.
        
        Args:
            src: Source fit wrapper
            distance: Distance in meters
            turret_cache: Pre-computed turret data
            missile_cache: Pre-computed missile data
            tracking_base: Base tracking params dict, or None for perfect tracking
            tgt: Target wrapper (for tracking-aware mode)
            commonData: Common data dict (for tracking-aware mode)
        """
        total_dps = 0
        ammo_name = None
        
        # Process turrets
        if turret_cache:
            for group_key, group_info in turret_cache.items():
                transitions = group_info['transitions']
                charge_data = group_info['charge_data']
                count = group_info['count']
                
                # Tracking-aware ammo selection
                turret_base = group_info.get('turret_base')
                if turret_base and tracking_base and commonData:
                    dps, name = get_dps_at_distance_with_projected(
                        transitions, charge_data, turret_base, distance,
                        tracking_base, src, tgt, commonData)
                else:
                    dps, name = get_dps_at_distance_tracking_aware(
                        transitions, charge_data, turret_base, distance, tracking_base)
                
                total_dps += dps * count
                if ammo_name is None:
                    ammo_name = name
        
        # Process missiles
        if missile_cache:
            tgtSpeed = tracking_base.get('tgtSpeed', 0) if tracking_base else 0
            tgtSigRadius = tracking_base.get('tgtSigRadius', 0) if tracking_base else 0
            
            for group_key, group_info in missile_cache.items():
                transitions = group_info['transitions']
                charge_data = group_info['charge_data']
                count = group_info['count']
                
                dps, name = get_missile_dps_at_distance(charge_data, transitions, distance, tgtSpeed, tgtSigRadius)
                total_dps += dps * count
                if ammo_name is None:
                    ammo_name = name
        
        return total_dps, ammo_name


class YOptimalAmmoVolleyMixin:
    """Calculate volley using optimal ammo selection."""

    def _getOptimalVolleyAtDistance(self, src, distance, turret_cache=None, missile_cache=None, tracking_base=None,
                                     tgt=None, commonData=None):
        """
        Get total volley with optimal ammo selection at a specific distance.
        Uses pre-computed transition points for O(log n) lookup.
        
        Args:
            src: Source fit wrapper
            distance: Distance in meters
            turret_cache: Pre-computed turret data
            missile_cache: Pre-computed missile data
            tracking_base: Base tracking params dict, or None for perfect tracking
            tgt: Target wrapper (for tracking-aware mode)
            commonData: Common data dict (for tracking-aware mode)
        """
        total_volley = 0
        
        # Process turrets
        if turret_cache:
            for group_key, group_info in turret_cache.items():
                transitions = group_info['transitions']
                charge_data = group_info['charge_data']
                count = group_info['count']
                
                # Tracking-aware ammo selection
                turret_base = group_info.get('turret_base')
                if turret_base and tracking_base and commonData:
                    volley, _ = get_volley_at_distance_with_projected(
                        transitions, charge_data, turret_base, distance,
                        tracking_base, src, tgt, commonData)
                else:
                    volley, _ = get_volley_at_distance_tracking_aware(
                        transitions, charge_data, turret_base, distance, tracking_base)
                
                total_volley += volley * count
        
        # Process missiles (use raw_volley * application factor)
        if missile_cache:
            tgtSpeed = tracking_base.get('tgtSpeed', 0) if tracking_base else 0
            tgtSigRadius = tracking_base.get('tgtSigRadius', 0) if tracking_base else 0
            
            for group_key, group_info in missile_cache.items():
                transitions = group_info['transitions']
                charge_data = group_info['charge_data']
                count = group_info['count']
                
                # Get DPS then convert to volley using raw_volley/raw_dps ratio
                dps, _ = get_missile_dps_at_distance(charge_data, transitions, distance, tgtSpeed, tgtSigRadius)
                # For missiles, volley = dps * cycle_time, but we don't have cycle time in the transitions
                # Use raw_volley/raw_dps ratio from charge_data
                if charge_data and charge_data[0]['raw_dps'] > 0:
                    ratio = charge_data[0]['raw_volley'] / charge_data[0]['raw_dps']
                    volley = dps * ratio
                else:
                    volley = 0
                total_volley += volley * count
        
        return total_volley

    def _getOptimalVolleyWithAmmoAtDistance(self, src, distance, turret_cache=None, missile_cache=None, tracking_base=None,
                                             tgt=None, commonData=None):
        """
        Get total volley and optimal ammo name at a specific distance.
        Returns (total_volley, ammo_name) tuple.
        
        Args:
            src: Source fit wrapper
            distance: Distance in meters
            turret_cache: Pre-computed turret data
            missile_cache: Pre-computed missile data
            tracking_base: Base tracking params dict, or None for perfect tracking
            tgt: Target wrapper (for tracking-aware mode)
            commonData: Common data dict (for tracking-aware mode)
        """
        total_volley = 0
        ammo_name = None
        
        # Process turrets
        if turret_cache:
            for group_key, group_info in turret_cache.items():
                transitions = group_info['transitions']
                charge_data = group_info['charge_data']
                count = group_info['count']
                
                # Tracking-aware ammo selection
                turret_base = group_info.get('turret_base')
                if turret_base and tracking_base and commonData:
                    volley, name = get_volley_at_distance_with_projected(
                        transitions, charge_data, turret_base, distance,
                        tracking_base, src, tgt, commonData)
                else:
                    volley, name = get_volley_at_distance_tracking_aware(
                        transitions, charge_data, turret_base, distance, tracking_base)
                
                total_volley += volley * count
                if ammo_name is None:
                    ammo_name = name
        
        # Process missiles
        if missile_cache:
            tgtSpeed = tracking_base.get('tgtSpeed', 0) if tracking_base else 0
            tgtSigRadius = tracking_base.get('tgtSigRadius', 0) if tracking_base else 0
            
            for group_key, group_info in missile_cache.items():
                transitions = group_info['transitions']
                charge_data = group_info['charge_data']
                count = group_info['count']
                
                dps, name = get_missile_dps_at_distance(charge_data, transitions, distance, tgtSpeed, tgtSigRadius)
                if charge_data and charge_data[0]['raw_dps'] > 0:
                    ratio = charge_data[0]['raw_volley'] / charge_data[0]['raw_dps']
                    volley = dps * ratio
                else:
                    volley = 0
                total_volley += volley * count
                if ammo_name is None:
                    ammo_name = name
        
        return total_volley, ammo_name


class XDistanceMixin(SmoothPointGetter):
    """X axis: Distance in meters."""

    _baseResolution = 100  # 1km resolution over 100km range
    _extraDepth = 1

    def _getCommonData(self, miscParams, src, tgt):
        # Get ammo quality tier from graph (set by canvasPanel before drawing)
        quality_tier = getattr(self.graph, '_ammoQuality', 'all')
        
        # Log tracking mode once per fit
        if not hasattr(self.graph, '_tracking_mode_logged'):
            pyfalog.debug("[TRACKING] Tracking-aware ammo selection ENABLED")
            self.graph._tracking_mode_logged = True
        
        # Get target resists if not ignoring them
        ignore_resists = GraphSettings.getInstance().get('ammoOptimalIgnoreResists')
        if ignore_resists or tgt is None:
            tgt_resists = None
        else:
            tgt_resists = tgt.getResists()  # (em, therm, kin, explo) in 0-1 range
        
        # Get projected effects setting
        applyProjected = GraphSettings.getInstance().get('ammoOptimalApplyProjected')
        
        # Get target application parameters for missiles (speed and sig radius)
        # These affect missile transitions, so they're part of the cache key
        tgtSpeed = miscParams.get('tgtSpeed', 0) or 0
        tgtSigRadius = tgt.getSigRadius() if tgt else 0
        
        # Cache on the GRAPH object (persists across getter instances)
        # Include quality tier, resists, projected setting, and target params in cache key
        cache_key = (id(src.item), quality_tier, tgt_resists, applyProjected, tgtSpeed, tgtSigRadius)
        
        # Initialize cache on graph if needed
        if not hasattr(self.graph, '_ammo_turret_cache'):
            self.graph._ammo_turret_cache = {}
            self.graph._ammo_missile_cache = {}
            self.graph._ammo_analysis_done = set()
        
        # Build common data dict
        commonData = {
            'src_radius': src.getRadius(),
            'applyProjected': applyProjected,
        }
        
        # Add projected effect data if enabled
        if applyProjected:
            commonData['srcScramRange'] = getScramRange(src=src)
            commonData['tgtScrammables'] = getScrammables(tgt=tgt) if tgt else ()
            # Get projected mod/drone/fighter data from cache
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
            commonData['turret_cache'] = self.graph._ammo_turret_cache[cache_key]
            commonData['missile_cache'] = self.graph._ammo_missile_cache.get(cache_key, {})
            return commonData
        
        # Run analysis once per fit (for logging purposes only)
        analysis_key = id(src.item)
        if analysis_key not in self.graph._ammo_analysis_done and hasattr(self, '_runFullAnalysis'):
            self.graph._ammo_analysis_done.add(analysis_key)
            self._runFullAnalysis(src)
        
        # Build turret cache with pre-computed transitions
        # NOTE: USE_TRACKING_AWARE_AMMO controls whether we use tracking-aware ammo selection
        # Set USE_TRACKING_AWARE_AMMO = False at top of file to revert to old behavior
        turret_cache = {}
        
        for mod in src.item.activeModulesIter():
            if mod.hardpoint != FittingHardpoint.TURRET:
                continue
            if mod.getModifiedItemAttr('miningAmount'):
                continue
            
            key = mod.item.ID
            if key not in turret_cache:
                turret_base = get_turret_base_stats(mod)
                cycle_params = mod.getCycleParameters()
                if cycle_params is None:
                    continue
                cycle_time_ms = cycle_params.averageTime
                
                # Get all valid charges and filter by quality tier
                all_charges = list(mod.getValidCharges())
                charges = filter_charges_by_quality(all_charges, quality_tier)
                if not charges:
                    continue
                
                # If using resists, filter projectile ammo by damage band
                # This optimization avoids testing redundant ammo types
                if tgt_resists:
                    charges = filter_projectile_by_band(charges, tgt_resists)
                
                # Get skill multiplier and compute charge data with resists applied
                skill_mult = get_charge_skill_multiplier(mod)
                
                # Use tracking-aware ammo selection
                # This properly accounts for tracking when choosing optimal ammo
                pyfalog.debug(f"[TRACKING] Building turret cache for module {mod.item.name} (ID={key})")
                
                charge_data = precompute_charge_data_with_tracking(
                    turret_base, charges, cycle_time_ms, skill_mult, tgt_resists)
                
                pyfalog.debug(f"[TRACKING] Pre-computed {len(charge_data)} charges with tracking data")
                
                # Build base tracking params dict for projected effect calculations
                # Note: At cache-build time, we use the same angles from miscParams that will
                # be used at lookup time. This ensures consistent tracking calculations.
                src_radius = src.getRadius()
                tgt_radius = tgt.getRadius() if tgt else 0
                
                # Use angles from miscParams (same as _buildTrackingParams uses at lookup)
                atkAngle = miscParams.get('atkAngle', 0) or 0
                tgtAngle = miscParams.get('tgtAngle', 0) or 0
                atkSpeed = miscParams.get('atkSpeed', 0) or 0
                
                pyfalog.debug(f"[TRACKING] Cache params: atkSpeed={atkSpeed}, atkAngle={atkAngle}, tgtSpeed={tgtSpeed}, tgtAngle={tgtAngle}, tgtSig={tgtSigRadius}")
                
                base_tracking_params = {
                    'atkSpeed': atkSpeed,    # Use actual attacker speed from graph params
                    'atkAngle': atkAngle,    # Use actual attacker angle from graph params
                    'atkRadius': src_radius,
                    'tgtSpeed': tgtSpeed,
                    'tgtAngle': tgtAngle,    # Use actual target angle from graph params
                    'tgtRadius': tgt_radius,
                    'tgtSigRadius': tgtSigRadius
                }
                
                # Pre-compute transition points with tracking awareness
                # These transitions consider tracking, webs, TPs, etc.
                transitions = calculate_transition_points_with_projected(
                    charge_data, turret_base, base_tracking_params,
                    src, tgt, commonData)
                
                pyfalog.debug(f"[TRACKING] Computed {len(transitions)} tracking-aware transition points")
                
                turret_cache[key] = {
                    'charge_data': charge_data,
                    'transitions': transitions,
                    'count': 1,
                    # Store turret base stats for lookup-time calculations
                    'turret_base': turret_base,
                    # Also store shortcut references for backward compatibility
                    'tracking': turret_base['tracking'],
                    'optimalSigRadius': turret_base['optimalSigRadius']
                }
            else:
                turret_cache[key]['count'] += 1
        
        # Build missile cache with pre-computed transitions
        missile_cache = {}
        ship_radius = src.getRadius()
        
        # We'll extract multipliers once from the first missile launcher
        # by temporarily loading a charge if needed
        damage_mults = None
        flight_mults = None
        app_mults = None
        
        pyfalog.error(f"Building missile cache for fit, ship_radius={ship_radius}")
        
        for mod in src.item.activeModulesIter():
            if mod.hardpoint != FittingHardpoint.MISSILE:
                continue
            
            pyfalog.error(f"Found missile launcher: {mod.item.name} (ID={mod.item.ID})")
            
            key = mod.item.ID
            if key not in missile_cache:
                cycle_params = mod.getCycleParameters()
                if cycle_params is None:
                    pyfalog.error(f"Missile launcher {mod.item.name} has no cycle params")
                    continue
                cycle_time_ms = cycle_params.averageTime
                
                # Get all valid charges and filter by quality tier
                all_charges = list(mod.getValidCharges())
                pyfalog.error(f"  Found {len(all_charges)} valid charges, quality_tier={quality_tier}")
                charges = filter_charges_by_quality(all_charges, quality_tier)
                if not charges:
                    pyfalog.error("  No charges after filtering for quality tier")
                    continue
                pyfalog.error(f"  After filtering: {len(charges)} charges")
                
                # On first launcher, extract multipliers by temporarily loading a charge
                if damage_mults is None:
                    damage_mults, flight_mults, app_mults = get_missile_multipliers_with_temp_charge(
                        mod, charges, src.item)
                    pyfalog.error(f"  Extracted damage multipliers: em={damage_mults['emDamage']:.3f}, th={damage_mults['thermalDamage']:.3f}, kin={damage_mults['kineticDamage']:.3f}, exp={damage_mults['explosiveDamage']:.3f}")
                    pyfalog.error(f"  Flight multipliers: vel={flight_mults['maxVelocity']:.3f}, delay={flight_mults['explosionDelay']:.3f}")
                    pyfalog.error(f"  Application multipliers: eR={app_mults['aoeCloudSize']:.3f}, eV={app_mults['aoeVelocity']:.3f}, DRF={app_mults['aoeDamageReductionFactor']:.3f}")
                
                # Pre-compute charge data with multipliers
                charge_data = precompute_missile_charge_data(
                    mod, charges, cycle_time_ms, ship_radius,
                    damage_mults=damage_mults,
                    flight_mults=flight_mults,
                    app_mults=app_mults,
                    tgt_resists=tgt_resists)
                
                if not charge_data:
                    pyfalog.error("  No charge data generated")
                    continue
                
                # Pre-compute transition points (requires target speed/sig for application)
                transitions = calculate_missile_transition_points(
                    charge_data, tgtSpeed, tgtSigRadius)
                pyfalog.error(f"  Computed {len(transitions) if transitions else 0} transition points")
                
                missile_cache[key] = {
                    'charge_data': charge_data,
                    'transitions': transitions,
                    'count': 1
                }
            else:
                missile_cache[key]['count'] += 1
        
        pyfalog.error(f"Built missile cache with {len(missile_cache)} launcher types")
        
        # Cache on graph for future calls
        self.graph._ammo_turret_cache[cache_key] = turret_cache
        self.graph._ammo_missile_cache[cache_key] = missile_cache
        
        # Add caches to commonData and return
        commonData['turret_cache'] = turret_cache
        commonData['missile_cache'] = missile_cache
        return commonData

    def _buildTrackingParams(self, distance, miscParams, src, tgt, commonData):
        """
        Build tracking parameters dict if velocity vectors are specified.
        
        Returns None if no tracking should be applied (both speeds are 0),
        otherwise returns a dict with all tracking calculation parameters.
        
        If projected effects are enabled, applies webs and target painters.
        """
        # Get speeds (default to 0 if not specified)
        atkSpeed = miscParams.get('atkSpeed', 0) or 0
        tgtSpeed = miscParams.get('tgtSpeed', 0) or 0
        
        # Get target signature radius
        tgtSigRadius = tgt.getSigRadius() if tgt else 0
        if tgtSigRadius == 0:
            return None  # Can't calculate tracking without target sig
        
        # Apply projected effects (webs and target painters) if enabled
        if commonData.get('applyProjected'):
            tgtSpeed = getTackledSpeed(
                src=src,
                tgt=tgt,
                currentUntackledSpeed=tgtSpeed,
                srcScramRange=commonData.get('srcScramRange'),
                tgtScrammables=commonData.get('tgtScrammables', ()),
                webMods=commonData.get('webMods', ()),
                webDrones=commonData.get('webDrones', ()),
                webFighters=commonData.get('webFighters', ()),
                distance=distance)
            tgtSigRadius = tgtSigRadius * getSigRadiusMult(
                src=src,
                tgt=tgt,
                tgtSpeed=tgtSpeed,
                srcScramRange=commonData.get('srcScramRange'),
                tgtScrammables=commonData.get('tgtScrammables', ()),
                tpMods=commonData.get('tpMods', ()),
                tpDrones=commonData.get('tpDrones', ()),
                tpFighters=commonData.get('tpFighters', ()),
                distance=distance)
        
        # Optimization: if both speeds are 0, no angular velocity, perfect tracking
        # But we still need to return params if sig radius was modified by TPs
        if atkSpeed == 0 and tgtSpeed == 0 and not commonData.get('applyProjected'):
            return None
        
        # Get angles (default to 0)
        atkAngle = miscParams.get('atkAngle', 0) or 0
        tgtAngle = miscParams.get('tgtAngle', 0) or 0
        
        tgtRadius = tgt.getRadius() if tgt else 0
        srcRadius = commonData.get('src_radius', 0)
        
        return {
            'atkSpeed': atkSpeed,
            'atkAngle': atkAngle,
            'atkRadius': srcRadius,
            'tgtSpeed': tgtSpeed,
            'tgtAngle': tgtAngle,
            'tgtRadius': tgtRadius,
            'tgtSigRadius': tgtSigRadius
        }

    def _calculatePoint(self, x, miscParams, src, tgt, commonData):
        """Calculate DPS/volley at distance x."""
        distance = x
        turret_cache = commonData.get('turret_cache')
        missile_cache = commonData.get('missile_cache')
        
        # Build tracking params (None if no velocity, meaning perfect tracking)
        tracking_base = self._buildTrackingParams(distance, miscParams, src, tgt, commonData)
        
        if hasattr(self, '_getOptimalDpsAtDistance'):
            return self._getOptimalDpsAtDistance(src, distance, turret_cache, missile_cache, tracking_base,
                                                  tgt=tgt, commonData=commonData)
        elif hasattr(self, '_getOptimalVolleyAtDistance'):
            return self._getOptimalVolleyAtDistance(src, distance, turret_cache, missile_cache, tracking_base,
                                                     tgt=tgt, commonData=commonData)
        else:
            return 0

    def _calculatePointExtended(self, x, miscParams, src, tgt, commonData):
        """Calculate DPS/volley at distance x, returning (value, extra_info) tuple."""
        distance = x
        turret_cache = commonData.get('turret_cache')
        missile_cache = commonData.get('missile_cache')
        
        # Build tracking params (None if no velocity, meaning perfect tracking)
        tracking_base = self._buildTrackingParams(distance, miscParams, src, tgt, commonData)
        
        if hasattr(self, '_getOptimalDpsWithAmmoAtDistance'):
            dps, ammo_name = self._getOptimalDpsWithAmmoAtDistance(src, distance, turret_cache, missile_cache, tracking_base,
                                                                    tgt=tgt, commonData=commonData)
            return dps, {'ammo': ammo_name}
        elif hasattr(self, '_getOptimalVolleyWithAmmoAtDistance'):
            volley, ammo_name = self._getOptimalVolleyWithAmmoAtDistance(src, distance, turret_cache, missile_cache, tracking_base,
                                                                          tgt=tgt, commonData=commonData)
            return volley, {'ammo': ammo_name}
        elif hasattr(self, '_getOptimalDpsAtDistance'):
            return self._getOptimalDpsAtDistance(src, distance, turret_cache, missile_cache, tracking_base,
                                                  tgt=tgt, commonData=commonData), {}
        elif hasattr(self, '_getOptimalVolleyAtDistance'):
            return self._getOptimalVolleyAtDistance(src, distance, turret_cache, missile_cache, tracking_base,
                                                     tgt=tgt, commonData=commonData), {}
        else:
            return 0, {}


class Distance2OptimalAmmoDpsGetter(XDistanceMixin, YOptimalAmmoDpsMixin):
    """Distance vs Optimal Ammo DPS graph."""
    
    def getPointExtended(self, x, miscParams, src, tgt):
        """Get point value and extra info (like ammo name) at x."""
        commonData = self._getCommonData(miscParams=miscParams, src=src, tgt=tgt)
        return self._calculatePointExtended(x, miscParams, src, tgt, commonData)
    
    def getSegments(self, xRange, miscParams, src, tgt):
        """
        Get plot segments with ammo transition information.
        
        Returns list of segment dicts:
        [
            {
                'xs': [0, 15000, ...],  # x coordinates for this segment
                'ys': [500, 480, ...],  # y coordinates for this segment  
                'ammo': 'Conflagration L',  # ammo name for this segment
                'ammoIndex': 0  # index for color assignment
            },
            ...
        ]
        """
        tgt_name = tgt.name if tgt else 'None'
        pyfalog.debug(f'getSegments: src={src.name}, tgt={tgt_name}, xRange={xRange}')
        
        commonData = self._getCommonData(miscParams=miscParams, src=src, tgt=tgt)
        turret_cache = commonData.get('turret_cache', {})
        missile_cache = commonData.get('missile_cache', {})
        
        if not turret_cache and not missile_cache:
            pyfalog.debug(f'getSegments: no turret_cache or missile_cache for tgt={tgt_name}')
            return []
        
        # Collect transitions from turrets
        all_transitions = []
        primary_charge_data = None
        
        for group_key, group_info in turret_cache.items():
            transitions = group_info['transitions']
            charge_data = group_info['charge_data']
            
            if primary_charge_data is None:
                primary_charge_data = charge_data
                all_transitions = transitions
            break  # Use first turret group
        
        # If no turret transitions, try missiles
        if not all_transitions and missile_cache:
            for group_key, group_info in missile_cache.items():
                transitions = group_info['transitions']
                charge_data = group_info['charge_data']
                
                if primary_charge_data is None:
                    primary_charge_data = charge_data
                    all_transitions = transitions
                break  # Use first missile group
        
        if not all_transitions or not primary_charge_data:
            pyfalog.debug(f'getSegments: no transitions for tgt={tgt_name}')
            return []
        
        # Filter out transitions with no ammo name (zero DPS transitions)
        valid_transitions = [t for t in all_transitions if t[2] is not None]
        
        pyfalog.debug(f'getSegments: tgt={tgt_name}, all_transitions={len(all_transitions)}, valid_transitions={len(valid_transitions)}')
        for i, t in enumerate(valid_transitions[:5]):  # Log first 5
            pyfalog.debug(f'  Transition {i}: dist={t[0]}, ammo={t[2]}')
        
        if not valid_transitions:
            return []
        
        # Build a mapping of ammo name to index for consistent coloring
        ammo_to_index = {}
        index_counter = 0
        for t in valid_transitions:
            ammo_name = t[2]
            if ammo_name not in ammo_to_index:
                ammo_to_index[ammo_name] = index_counter
                index_counter += 1
        
        # Generate segments based on transitions
        segments = []
        min_x, max_x = xRange
        tgt_name = tgt.name if tgt else 'None'
        
        for i, transition in enumerate(valid_transitions):
            trans_dist = transition[0]  # Distance where this ammo becomes optimal
            ammo_name = transition[2]
            
            # Determine segment range
            seg_start = max(trans_dist, min_x)
            
            # Find end of segment (next transition or max_x)
            if i + 1 < len(valid_transitions):
                next_trans_dist = valid_transitions[i + 1][0]
                seg_end = min(next_trans_dist, max_x)
            else:
                # Last segment - extend to where DPS drops to near zero
                # Find the None transition (zero DPS) to limit the range
                zero_transition = next((t for t in all_transitions if t[2] is None), None)
                if zero_transition:
                    seg_end = min(zero_transition[0], max_x)
                else:
                    seg_end = max_x
            
            # Skip if segment is outside range or empty
            if seg_start >= max_x or seg_end <= min_x or seg_start >= seg_end:
                pyfalog.debug(f'getSegments DPS: SKIPPING segment {i} for tgt={tgt_name}, ammo={ammo_name}, seg_start={seg_start}, seg_end={seg_end}, min_x={min_x}, max_x={max_x}')
                continue
            
            # Generate points for this segment
            # Use adaptive resolution for smooth curves
            num_points = max(20, int((seg_end - seg_start) / 500))  # Point every 500m minimum
            xs = []
            ys = []
            
            for j in range(num_points + 1):
                x = seg_start + (seg_end - seg_start) * j / num_points
                y = self._calculatePoint(x, miscParams, src, tgt, commonData)
                xs.append(x)
                ys.append(y)
            
            pyfalog.debug(f'getSegments DPS: ADDED segment {i} for tgt={tgt_name}, ammo={ammo_name}, xs=[{xs[0]:.0f}..{xs[-1]:.0f}], ys=[{ys[0]:.1f}..{ys[-1]:.1f}]')
            
            segments.append({
                'xs': xs,
                'ys': ys,
                'ammo': ammo_name,
                'ammoIndex': ammo_to_index[ammo_name]
            })
        
        pyfalog.debug(f'getSegments DPS: returning {len(segments)} segments for tgt={tgt_name}')
        return segments


class Distance2OptimalAmmoVolleyGetter(XDistanceMixin, YOptimalAmmoVolleyMixin):
    """Distance vs Optimal Ammo Volley graph."""
    
    def getPointExtended(self, x, miscParams, src, tgt):
        """Get point value and extra info (like ammo name) at x."""
        commonData = self._getCommonData(miscParams=miscParams, src=src, tgt=tgt)
        return self._calculatePointExtended(x, miscParams, src, tgt, commonData)
    
    def getSegments(self, xRange, miscParams, src, tgt):
        """
        Get plot segments with ammo transition information for volley.
        
        Returns list of segment dicts:
        [
            {
                'xs': [0, 15000, ...],  # x coordinates for this segment
                'ys': [1000, 960, ...],  # y coordinates for this segment (volley)
                'ammo': 'Conflagration L',  # ammo name for this segment
                'ammoIndex': 0  # index for color assignment
            },
            ...
        ]
        """
        commonData = self._getCommonData(miscParams=miscParams, src=src, tgt=tgt)
        turret_cache = commonData.get('turret_cache', {})
        missile_cache = commonData.get('missile_cache', {})
        
        if not turret_cache and not missile_cache:
            return []
        
        # Collect transitions from turrets
        all_transitions = []
        primary_charge_data = None
        
        for group_key, group_info in turret_cache.items():
            transitions = group_info['transitions']
            charge_data = group_info['charge_data']
            
            if primary_charge_data is None:
                primary_charge_data = charge_data
                all_transitions = transitions
            break  # Use first turret group
        
        # If no turret transitions, try missiles
        if not all_transitions and missile_cache:
            for group_key, group_info in missile_cache.items():
                transitions = group_info['transitions']
                charge_data = group_info['charge_data']
                
                if primary_charge_data is None:
                    primary_charge_data = charge_data
                    all_transitions = transitions
                break  # Use first missile group
        
        if not all_transitions or not primary_charge_data:
            return []
        
        # Filter out transitions with no ammo name (zero DPS transitions)
        valid_transitions = [t for t in all_transitions if t[2] is not None]
        
        if not valid_transitions:
            return []
        
        # Build a mapping of ammo name to index for consistent coloring
        ammo_to_index = {}
        index_counter = 0
        for t in valid_transitions:
            ammo_name = t[2]
            if ammo_name not in ammo_to_index:
                ammo_to_index[ammo_name] = index_counter
                index_counter += 1
        
        # Generate segments based on transitions
        segments = []
        min_x, max_x = xRange
        
        for i, transition in enumerate(valid_transitions):
            trans_dist = transition[0]  # Distance where this ammo becomes optimal
            ammo_name = transition[2]
            
            # Determine segment range
            seg_start = max(trans_dist, min_x)
            
            # Find end of segment (next transition or max_x)
            if i + 1 < len(valid_transitions):
                next_trans_dist = valid_transitions[i + 1][0]
                seg_end = min(next_trans_dist, max_x)
            else:
                # Last segment - extend to where DPS drops to near zero
                # Find the None transition (zero DPS) to limit the range
                zero_transition = next((t for t in all_transitions if t[2] is None), None)
                if zero_transition:
                    seg_end = min(zero_transition[0], max_x)
                else:
                    seg_end = max_x
            
            # Skip if segment is outside range or empty
            if seg_start >= max_x or seg_end <= min_x or seg_start >= seg_end:
                continue
            
            # Generate points for this segment
            # Use adaptive resolution for smooth curves
            num_points = max(20, int((seg_end - seg_start) / 500))  # Point every 500m minimum
            xs = []
            ys = []
            
            for j in range(num_points + 1):
                x = seg_start + (seg_end - seg_start) * j / num_points
                y = self._calculatePoint(x, miscParams, src, tgt, commonData)
                xs.append(x)
                ys.append(y)
            
            segments.append({
                'xs': xs,
                'ys': ys,
                'ammo': ammo_name,
                'ammoIndex': ammo_to_index[ammo_name]
            })
        
        return segments

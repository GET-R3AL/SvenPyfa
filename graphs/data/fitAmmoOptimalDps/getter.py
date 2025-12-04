# =============================================================================
# Copyright (C) 2010 Diego Duclos
#
# This file is part of pyfa.
#
# pyfa is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# pyfa is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with pyfa.  If not, see <http://www.gnu.org/licenses/>.
# =============================================================================

"""
Optimal Ammo DPS Graph - Shows best ammo choice at each distance.

Simplified turret-only version for testing:
1. Get active turrets with ship/skill bonuses applied
2. Get all compatible charges  
3. For each distance, find which charge gives best DPS (range-applied)

Optimizations:
1. Pre-compute raw DPS and effective range for each charge once
2. Sort charges by raw DPS descending - highest damage first
3. Calculate transition points (where optimal ammo changes) ONCE
4. Use binary search to find relevant ammo at any distance
5. Apply skill bonus multiplier from loaded charge to all charges
6. Apply target resists early in the pipeline (multiply damage by 1-resist)
7. Group projectile ammo by damage band to reduce redundant calculations
"""

import math
import re
from bisect import bisect_right
from functools import lru_cache
from logbook import Logger

from eos.calc import calculateRangeFactor
from eos.const import FittingHardpoint
from graphs.data.base import SmoothPointGetter
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
    
    If a charge is loaded, it affects maxRange and falloff via multipliers.
    We need to undo those effects to get the true base turret stats.
    
    Returns dict with: optimal, falloff, tracking, optimalSigRadius, damageMultiplier
    """
    # Get the modified values (includes charge effects if charge is loaded)
    optimal = module.getModifiedItemAttr('maxRange') or 0
    falloff = module.getModifiedItemAttr('falloff') or 0
    tracking = module.getModifiedItemAttr('trackingSpeed') or 0
    optimal_sig_radius = module.getModifiedItemAttr('optimalSigRadius') or 0
    damage_mult = module.getModifiedItemAttr('damageMultiplier') or 1
    
    # If a charge is loaded, undo its range/falloff multiplier effects
    # Charges multiply these stats, so we divide them out
    if module.charge:
        charge_range_mult = module.charge.getAttribute('weaponRangeMultiplier') or 1
        charge_falloff_mult = module.charge.getAttribute('fallofMultiplier') or 1  # EVE typo
        
        if charge_range_mult != 0:
            optimal = optimal / charge_range_mult
        if charge_falloff_mult != 0:
            falloff = falloff / charge_falloff_mult
    
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


def precompute_charge_data(turret_base, charges, cycle_time_ms, skill_multiplier=1.0, tgt_resists=None):
    """
    Pre-compute constant values for each charge.
    
    Returns list of dicts sorted by raw_dps DESCENDING (highest damage first).
    Each dict has: name, raw_dps, raw_volley, effective_optimal, effective_falloff
    
    Sorting enables pruning optimization: once a high-DPS charge loses,
    we can skip it for all greater distances.
    
    Args:
        turret_base: Base turret stats (optimal, falloff, damageMultiplier)
        charges: List of charge items
        cycle_time_ms: Turret cycle time in milliseconds
        skill_multiplier: Multiplier for charge damage from skills (default 1.0)
        tgt_resists: Target resist tuple (em, therm, kin, explo) in 0-1 range, or None to ignore
    """
    charge_data = []
    for charge in charges:
        charge_stats = get_charge_stats(charge)
        
        # Apply target resists if provided (early in pipeline for efficiency)
        if tgt_resists:
            charge_stats = apply_resists_to_charge_stats(charge_stats, tgt_resists)
        
        # These are constant regardless of distance
        effective_optimal = turret_base['optimal'] * charge_stats['rangeMultiplier']
        effective_falloff = turret_base['falloff'] * charge_stats['falloffMultiplier']
        
        # Apply skill multiplier to charge damage
        adjusted_damage = charge_stats['totalDamage'] * skill_multiplier
        raw_volley = adjusted_damage * turret_base['damageMultiplier']
        raw_dps = raw_volley / (cycle_time_ms / 1000)
        
        charge_data.append({
            'name': charge.name,
            'raw_dps': raw_dps,
            'raw_volley': raw_volley,
            'effective_optimal': effective_optimal,
            'effective_falloff': effective_falloff
        })
    
    # Sort by raw_dps descending - highest damage charges first
    # This enables early termination when best charge is within optimal
    charge_data.sort(key=lambda x: x['raw_dps'], reverse=True)
    
    return charge_data


def calculate_best_dps_at_distance(charge_data, distance, start_index=0):
    """
    Find the best charge for a turret at a specific distance.
    Uses pre-computed charge data (sorted by raw_dps descending) for efficiency.

    Args:
        charge_data: List of charge dicts, sorted by raw_dps descending
        distance: Distance in meters
        start_index: Index to start searching from (pruned charges before this)
    
    Returns: (best_dps, best_charge_name, new_start_index)
             new_start_index is the index of the winning charge for pruning
    """
    best_dps = 0
    best_charge_name = None
    best_index = start_index
    
    for i in range(start_index, len(charge_data)):
        cd = charge_data[i]
        
        # Optimization: if we're within optimal range, range_factor = 1.0
        # With perfect tracking, chanceToHit = 1.0, and turretMult ≈ 1.0
        if distance <= cd['effective_optimal']:
            # Within optimal: CTH = 1.0, turret mult ≈ 1.0 (actually ~1.005 due to wrecking)
            turret_mult = calcTurretDamageMult(1.0)
            effective_dps = cd['raw_dps'] * turret_mult
            
            if effective_dps > best_dps:
                best_dps = effective_dps
                best_charge_name = cd['name']
                best_index = i
                # Since charges are sorted by raw_dps descending, and we're within
                # optimal (all get same turret_mult), no subsequent charge can beat this
                break
        else:
            # Outside optimal, calculate range factor then turret damage multiplier
            # With perfect tracking: chanceToHit = rangeFactor
            # restrictedRange=False: turrets can fire at any range (damage just falls off)
            range_factor = calculateRangeFactor(cd['effective_optimal'], cd['effective_falloff'], distance, restrictedRange=False)
            turret_mult = calcTurretDamageMult(range_factor)
            effective_dps = cd['raw_dps'] * turret_mult
            
            if effective_dps > best_dps:
                best_dps = effective_dps
                best_charge_name = cd['name']
                best_index = i
    
    return best_dps, best_charge_name, best_index


def calculate_transition_points(charge_data, max_distance=300000, resolution=100):
    """
    Calculate the distances where optimal ammo changes.
    
    Returns list of tuples: [(distance, charge_index, charge_name, dps), ...]
    sorted by distance ascending.
    
    This is calculated ONCE and then used for O(log n) lookups.
    """
    if not charge_data:
        return []
    
    transitions = []
    current_index = 0
    current_charge = charge_data[0]['name']
    
    # Start at distance 0
    best_dps, best_name, best_idx = calculate_best_dps_at_distance(charge_data, 0, 0)
    transitions.append((0, best_idx, best_name, best_dps))
    current_index = best_idx
    current_charge = best_name
    
    # Scan through distances to find transitions
    # Use larger steps initially, then refine near transitions
    distance = resolution
    while distance <= max_distance:
        best_dps, best_name, best_idx = calculate_best_dps_at_distance(
            charge_data, distance, current_index
        )
        
        if best_name != current_charge:
            # Found a transition - binary search to find exact point
            low = distance - resolution
            high = distance
            while high - low > 10:  # 10m precision
                mid = (low + high) // 2
                _, mid_name, _ = calculate_best_dps_at_distance(
                    charge_data, mid, current_index
                )
                if mid_name == current_charge:
                    low = mid
                else:
                    high = mid
            
            # Record the transition
            transitions.append((high, best_idx, best_name, best_dps))
            current_index = best_idx
            current_charge = best_name
        
        # If DPS is effectively zero, stop
        if best_dps < 0.01:
            break
            
        distance += resolution
    
    return transitions


def get_dps_at_distance_fast(transitions, charge_data, distance, tracking_params=None):
    """
    Fast O(log n) lookup of DPS at a specific distance using pre-computed transitions.
    
    Uses full turret damage formula (with wrecking shots).
    Optionally applies tracking factor if tracking_params is provided.
    
    Args:
        transitions: List of (distance, charge_index, charge_name, dps) tuples
        charge_data: Pre-computed charge data (for recalculating DPS at exact distance)
        distance: Distance in meters
        tracking_params: Optional dict with tracking parameters:
            - atkSpeed: Attacker absolute speed (m/s)
            - atkAngle: Attacker movement angle (degrees)
            - atkRadius: Attacker ship radius
            - tgtSpeed: Target absolute speed (m/s)
            - tgtAngle: Target movement angle (degrees)
            - tgtRadius: Target ship radius
            - tgtSigRadius: Target signature radius
            - turretTracking: Turret tracking speed
            - turretOptimalSigRadius: Turret optimal signature radius
    
    Returns: (dps, charge_name)
    """
    if not transitions:
        return 0, None
    
    # Find the transition that applies at this distance
    # bisect_right finds insertion point, so we need index - 1
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
    if distance <= cd['effective_optimal']:
        range_factor = 1.0
    else:
        range_factor = calculateRangeFactor(cd['effective_optimal'], cd['effective_falloff'], distance, restrictedRange=False)
    
    # Calculate tracking factor if params provided
    if tracking_params:
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
            tracking_params['turretTracking'],
            tracking_params['turretOptimalSigRadius'],
            angular_speed,
            tracking_params['tgtSigRadius']
        )
    else:
        tracking_factor = 1.0  # Perfect tracking
    
    # chanceToHit = rangeFactor * trackingFactor
    cth = range_factor * tracking_factor
    turret_mult = calcTurretDamageMult(cth)
    dps = cd['raw_dps'] * turret_mult
    
    return dps, cd['name']


def get_volley_at_distance_fast(transitions, charge_data, distance, tracking_params=None):
    """
    Fast O(log n) lookup of volley at a specific distance using pre-computed transitions.
    
    Same as get_dps_at_distance_fast but uses raw_volley instead of raw_dps.
    
    Args:
        transitions: List of (distance, charge_index, charge_name, dps) tuples
        charge_data: Pre-computed charge data (for recalculating at exact distance)
        distance: Distance in meters
        tracking_params: Optional dict with tracking parameters (see get_dps_at_distance_fast)
    
    Returns: (volley, charge_name)
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
    
    # Calculate exact volley at this distance using that charge
    cd = charge_data[charge_idx]
    
    # Calculate range factor
    if distance <= cd['effective_optimal']:
        range_factor = 1.0
    else:
        range_factor = calculateRangeFactor(cd['effective_optimal'], cd['effective_falloff'], distance, restrictedRange=False)
    
    # Calculate tracking factor if params provided
    if tracking_params:
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
            tracking_params['turretTracking'],
            tracking_params['turretOptimalSigRadius'],
            angular_speed,
            tracking_params['tgtSigRadius']
        )
    else:
        tracking_factor = 1.0  # Perfect tracking
    
    # chanceToHit = rangeFactor * trackingFactor
    cth = range_factor * tracking_factor
    turret_mult = calcTurretDamageMult(cth)
    volley = cd['raw_volley'] * turret_mult
    
    return volley, cd['name']


def get_ammo_name_at_distance_fast(transitions, distance):
    """
    Ultra-fast O(log n) lookup of just the ammo name at a distance.
    
    No DPS calculation - just returns which ammo is optimal.
    Used for UI display during drag operations.
    
    Args:
        transitions: List of (distance, charge_index, charge_name, dps) tuples
        distance: Distance in meters
    
    Returns: charge_name (str) or None
    """
    if not transitions:
        return None
    
    # Find the transition that applies at this distance
    distances = [t[0] for t in transitions]
    idx = bisect_right(distances, distance) - 1
    if idx < 0:
        idx = 0
    
    # Return just the charge name (index 2 in the tuple)
    return transitions[idx][2]


class YOptimalAmmoDpsMixin:
    """Calculate DPS using optimal ammo selection for turrets."""

    def _getOptimalDpsAtDistance(self, src, distance, turret_cache=None, tracking_base=None):
        """
        Get total DPS with optimal ammo selection at a specific distance.
        Uses pre-computed transition points for O(log n) lookup.
        
        Args:
            src: Source fit wrapper
            distance: Distance in meters
            turret_cache: Pre-computed turret data (charge_data, transitions, tracking stats)
            tracking_base: Base tracking params dict (without turret-specific stats), or None for perfect tracking
        """
        total_dps = 0
        
        # Use cached data if available (from _getCommonData)
        if turret_cache:
            for group_key, group_info in turret_cache.items():
                transitions = group_info['transitions']
                charge_data = group_info['charge_data']
                count = group_info['count']
                
                # Build full tracking params by adding turret-specific stats
                tracking_params = None
                if tracking_base is not None:
                    tracking_params = tracking_base.copy()
                    tracking_params['turretTracking'] = group_info.get('tracking', 0)
                    tracking_params['turretOptimalSigRadius'] = group_info.get('optimalSigRadius', 0)
                
                dps, _ = get_dps_at_distance_fast(transitions, charge_data, distance, tracking_params)
                total_dps += dps * count
            return total_dps
        
        # Fallback: compute on the fly (shouldn't happen with proper caching)
        turret_groups = {}
        
        for mod in src.item.activeModulesIter():
            if mod.hardpoint != FittingHardpoint.TURRET:
                continue
            if mod.getModifiedItemAttr('miningAmount'):
                continue
                
            key = mod.item.ID
            if key not in turret_groups:
                turret_groups[key] = {'module': mod, 'count': 1}
            else:
                turret_groups[key]['count'] += 1
        
        for group_data in turret_groups.values():
            mod = group_data['module']
            count = group_data['count']
            
            turret_base = get_turret_base_stats(mod)
            cycle_params = mod.getCycleParameters()
            if cycle_params is None:
                continue
            cycle_time_ms = cycle_params.averageTime
            
            charges = list(mod.getValidCharges())
            if not charges:
                continue
            
            skill_mult = get_charge_skill_multiplier(mod)
            charge_data = precompute_charge_data(turret_base, charges, cycle_time_ms, skill_mult)
            best_dps, _, _ = calculate_best_dps_at_distance(charge_data, distance)
            total_dps += best_dps * count
        
        return total_dps

    def _getOptimalDpsWithAmmoAtDistance(self, src, distance, turret_cache=None, tracking_base=None):
        """
        Get total DPS and optimal ammo name at a specific distance.
        Returns (total_dps, ammo_name) tuple.
        
        Args:
            src: Source fit wrapper
            distance: Distance in meters
            turret_cache: Pre-computed turret data
            tracking_base: Base tracking params dict, or None for perfect tracking
        """
        total_dps = 0
        ammo_name = None
        
        # Use cached data if available (from _getCommonData)
        if turret_cache:
            for group_key, group_info in turret_cache.items():
                transitions = group_info['transitions']
                charge_data = group_info['charge_data']
                count = group_info['count']
                
                # Build full tracking params by adding turret-specific stats
                tracking_params = None
                if tracking_base is not None:
                    tracking_params = tracking_base.copy()
                    tracking_params['turretTracking'] = group_info.get('tracking', 0)
                    tracking_params['turretOptimalSigRadius'] = group_info.get('optimalSigRadius', 0)
                
                dps, name = get_dps_at_distance_fast(transitions, charge_data, distance, tracking_params)
                total_dps += dps * count
                # Use the first turret group's ammo name (most common case is single turret type)
                if ammo_name is None:
                    ammo_name = name
            return total_dps, ammo_name
        
        return total_dps, None
    
    def _runFullAnalysis(self, src):
        """
        Run full analysis with pre-computed charge data and transitions.
        Called once when graph is first calculated.
        """
        pyfalog.error("=== Starting Optimal Ammo Analysis ===")
        
        # Build turret groups and pre-compute charge data
        turret_groups = {}
        turret_count = 0
        
        for mod in src.item.activeModulesIter():
            if mod.hardpoint != FittingHardpoint.TURRET:
                continue
            if mod.getModifiedItemAttr('miningAmount'):
                continue
            
            turret_count += 1
            key = mod.item.ID
            
            if key not in turret_groups:
                turret_base = get_turret_base_stats(mod)
                cycle_params = mod.getCycleParameters()
                if cycle_params is None:
                    pyfalog.error(f"  WARNING: No cycle params for {mod.item.name}")
                    continue
                cycle_time_ms = cycle_params.averageTime
                charges = list(mod.getValidCharges())
                
                # Get skill multiplier for charge damage
                skill_mult = get_charge_skill_multiplier(mod)
                pyfalog.error(f"  Skill multiplier for {mod.item.name}: {skill_mult:.3f}")
                
                # Pre-compute charge data ONCE per turret type
                charge_data = precompute_charge_data(turret_base, charges, cycle_time_ms, skill_mult)
                
                # Calculate transition points
                transitions = calculate_transition_points(charge_data)
                
                turret_groups[key] = {
                    'name': mod.item.name,
                    'count': 1,
                    'turret_base': turret_base,
                    'cycle_time_ms': cycle_time_ms,
                    'charge_data': charge_data,
                    'transitions': transitions,
                    'num_charges': len(charges)
                }
            else:
                turret_groups[key]['count'] += 1
        
        if not turret_groups:
            pyfalog.error("WARNING: No turrets found on fit!")
            return
        
        # Log turret info and transitions
        for key, info in turret_groups.items():
            pyfalog.error(f"Turret: {info['name']} x{info['count']}")
            pyfalog.error(f"  Base Optimal: {info['turret_base']['optimal']/1000:.1f}km")
            pyfalog.error(f"  Base Falloff: {info['turret_base']['falloff']/1000:.1f}km")
            pyfalog.error(f"  Compatible Charges: {info['num_charges']}")
            pyfalog.error(f"  Transition points: {len(info['transitions'])}")
            for t in info['transitions']:
                dist_km = t[0] / 1000
                pyfalog.error(f"    {dist_km:.1f}km: {t[2]} ({t[3]:.1f} DPS)")
        
        pyfalog.error("=== Analysis Complete ===")


class YOptimalAmmoVolleyMixin:
    """Calculate volley using optimal ammo selection."""

    def _getOptimalVolleyAtDistance(self, src, distance, turret_cache=None, tracking_base=None):
        """
        Get total volley with optimal ammo selection at a specific distance.
        Uses pre-computed transition points for O(log n) lookup.
        
        Args:
            src: Source fit wrapper
            distance: Distance in meters
            turret_cache: Pre-computed turret data
            tracking_base: Base tracking params dict, or None for perfect tracking
        """
        total_volley = 0
        
        # Use cached data if available (from _getCommonData)
        if turret_cache:
            for group_key, group_info in turret_cache.items():
                transitions = group_info['transitions']
                charge_data = group_info['charge_data']
                count = group_info['count']
                
                # Build full tracking params by adding turret-specific stats
                tracking_params = None
                if tracking_base is not None:
                    tracking_params = tracking_base.copy()
                    tracking_params['turretTracking'] = group_info.get('tracking', 0)
                    tracking_params['turretOptimalSigRadius'] = group_info.get('optimalSigRadius', 0)
                
                volley, _ = get_volley_at_distance_fast(transitions, charge_data, distance, tracking_params)
                total_volley += volley * count
            return total_volley
        
        return total_volley

    def _getOptimalVolleyWithAmmoAtDistance(self, src, distance, turret_cache=None, tracking_base=None):
        """
        Get total volley and optimal ammo name at a specific distance.
        Returns (total_volley, ammo_name) tuple.
        
        Args:
            src: Source fit wrapper
            distance: Distance in meters
            turret_cache: Pre-computed turret data
            tracking_base: Base tracking params dict, or None for perfect tracking
        """
        total_volley = 0
        ammo_name = None
        
        # Use cached data if available (from _getCommonData)
        if turret_cache:
            for group_key, group_info in turret_cache.items():
                transitions = group_info['transitions']
                charge_data = group_info['charge_data']
                count = group_info['count']
                
                # Build full tracking params by adding turret-specific stats
                tracking_params = None
                if tracking_base is not None:
                    tracking_params = tracking_base.copy()
                    tracking_params['turretTracking'] = group_info.get('tracking', 0)
                    tracking_params['turretOptimalSigRadius'] = group_info.get('optimalSigRadius', 0)
                
                volley, name = get_volley_at_distance_fast(transitions, charge_data, distance, tracking_params)
                total_volley += volley * count
                # Use the first turret group's ammo name (most common case is single turret type)
                if ammo_name is None:
                    ammo_name = name
            return total_volley, ammo_name
        
        return total_volley, None


class XDistanceMixin(SmoothPointGetter):
    """X axis: Distance in meters."""

    _baseResolution = 100  # 1km resolution over 100km range
    _extraDepth = 1

    def _getCommonData(self, miscParams, src, tgt):
        # Get ammo quality tier from graph (set by canvasPanel before drawing)
        quality_tier = getattr(self.graph, '_ammoQuality', 'all')
        
        # Get target resists if not ignoring them
        ignore_resists = GraphSettings.getInstance().get('ammoOptimalIgnoreResists')
        if ignore_resists or tgt is None:
            tgt_resists = None
        else:
            tgt_resists = tgt.getResists()  # (em, therm, kin, explo) in 0-1 range
        
        # Cache on the GRAPH object (persists across getter instances)
        # Include quality tier and resists in cache key so different settings get different caches
        cache_key = (id(src.item), quality_tier, tgt_resists)
        
        # Initialize cache on graph if needed
        if not hasattr(self.graph, '_ammo_turret_cache'):
            self.graph._ammo_turret_cache = {}
            self.graph._ammo_analysis_done = set()
        
        # Return cached data if available
        if cache_key in self.graph._ammo_turret_cache:
            return {'turret_cache': self.graph._ammo_turret_cache[cache_key], 'src_radius': src.getRadius()}
        
        # Run analysis once per fit (for logging purposes only)
        analysis_key = id(src.item)
        if analysis_key not in self.graph._ammo_analysis_done and hasattr(self, '_runFullAnalysis'):
            self.graph._ammo_analysis_done.add(analysis_key)
            self._runFullAnalysis(src)
        
        # Build turret cache with pre-computed transitions
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
                charge_data = precompute_charge_data(
                    turret_base, charges, cycle_time_ms, skill_mult, tgt_resists)
                
                # Pre-compute transition points (this is the key optimization!)
                transitions = calculate_transition_points(charge_data)
                
                turret_cache[key] = {
                    'charge_data': charge_data,
                    'transitions': transitions,
                    'count': 1,
                    # Store turret tracking stats for later use
                    'tracking': turret_base['tracking'],
                    'optimalSigRadius': turret_base['optimalSigRadius']
                }
            else:
                turret_cache[key]['count'] += 1
        
        # Cache on graph for future calls
        self.graph._ammo_turret_cache[cache_key] = turret_cache
        
        # Store source ship radius in commonData for tracking calculations
        return {'turret_cache': turret_cache, 'src_radius': src.getRadius()}

    def _buildTrackingParams(self, distance, miscParams, tgt, commonData):
        """
        Build tracking parameters dict if velocity vectors are specified.
        
        Returns None if no tracking should be applied (both speeds are 0),
        otherwise returns a dict with all tracking calculation parameters.
        """
        # Get speeds (default to 0 if not specified)
        atkSpeed = miscParams.get('atkSpeed', 0) or 0
        tgtSpeed = miscParams.get('tgtSpeed', 0) or 0
        
        # Optimization: if both speeds are 0, no angular velocity, perfect tracking
        if atkSpeed == 0 and tgtSpeed == 0:
            return None
        
        # Get angles (default to 0)
        atkAngle = miscParams.get('atkAngle', 0) or 0
        tgtAngle = miscParams.get('tgtAngle', 0) or 0
        
        # Get target signature radius and ship radius
        tgtSigRadius = miscParams.get('tgtSigRad')
        if tgtSigRadius is None and tgt is not None:
            tgtSigRadius = tgt.getSigRadius()
        if tgtSigRadius is None or tgtSigRadius == 0:
            return None  # Can't calculate tracking without target sig
        
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
        
        # Build tracking params (None if no velocity, meaning perfect tracking)
        tracking_base = self._buildTrackingParams(distance, miscParams, tgt, commonData)
        
        if hasattr(self, '_getOptimalDpsAtDistance'):
            return self._getOptimalDpsAtDistance(src, distance, turret_cache, tracking_base)
        elif hasattr(self, '_getOptimalVolleyAtDistance'):
            return self._getOptimalVolleyAtDistance(src, distance, turret_cache, tracking_base)
        else:
            return 0

    def _calculatePointExtended(self, x, miscParams, src, tgt, commonData):
        """Calculate DPS/volley at distance x, returning (value, extra_info) tuple."""
        distance = x
        turret_cache = commonData.get('turret_cache')
        
        # Build tracking params (None if no velocity, meaning perfect tracking)
        tracking_base = self._buildTrackingParams(distance, miscParams, tgt, commonData)
        
        if hasattr(self, '_getOptimalDpsWithAmmoAtDistance'):
            dps, ammo_name = self._getOptimalDpsWithAmmoAtDistance(src, distance, turret_cache, tracking_base)
            return dps, {'ammo': ammo_name}
        elif hasattr(self, '_getOptimalVolleyWithAmmoAtDistance'):
            volley, ammo_name = self._getOptimalVolleyWithAmmoAtDistance(src, distance, turret_cache, tracking_base)
            return volley, {'ammo': ammo_name}
        elif hasattr(self, '_getOptimalDpsAtDistance'):
            return self._getOptimalDpsAtDistance(src, distance, turret_cache, tracking_base), {}
        elif hasattr(self, '_getOptimalVolleyAtDistance'):
            return self._getOptimalVolleyAtDistance(src, distance, turret_cache, tracking_base), {}
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
        
        if not turret_cache:
            pyfalog.debug(f'getSegments: no turret_cache for tgt={tgt_name}')
            return []
        
        # Collect all unique transitions across all turret groups
        # For simplicity, use the first turret group's transitions
        # (in most cases, all turrets are the same type)
        all_transitions = []
        primary_charge_data = None
        total_count = 0
        
        for group_key, group_info in turret_cache.items():
            transitions = group_info['transitions']
            charge_data = group_info['charge_data']
            count = group_info['count']
            
            if primary_charge_data is None:
                primary_charge_data = charge_data
                all_transitions = transitions
                total_count = count
            else:
                # Multiple turret types - just add counts
                total_count += count
        
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
        
        if not turret_cache:
            return []
        
        # Collect all unique transitions across all turret groups
        # For simplicity, use the first turret group's transitions
        # (in most cases, all turrets are the same type)
        all_transitions = []
        primary_charge_data = None
        total_count = 0
        
        for group_key, group_info in turret_cache.items():
            transitions = group_info['transitions']
            charge_data = group_info['charge_data']
            count = group_info['count']
            
            if primary_charge_data is None:
                primary_charge_data = charge_data
                all_transitions = transitions
                total_count = count
            else:
                # Multiple turret types - just add counts
                total_count += count
        
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

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
Turret mechanics calculations for optimal ammo selection.

This module contains EVE turret formulas for:
- Angular speed calculation
- Tracking factor calculation  
- Turret damage multiplier from chance to hit
- Base turret stats extraction (without charge modifiers)
- Skill multiplier extraction
"""

import math

from eos.calc import calculateRangeFactor


# =============================================================================
# Angular Speed
# =============================================================================

def calcAngularSpeed(atkSpeed, atkAngle, atkRadius, distance, tgtSpeed, tgtAngle, tgtRadius):
    """
    Calculate angular speed (rad/s) between attacker and target.
    
    Angular speed = transversal_velocity / center_to_center_distance
    
    Based on EVE formula:
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


# =============================================================================
# Tracking Factor
# =============================================================================

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


# =============================================================================
# Turret Damage Multiplier
# =============================================================================

def calcTurretDamageMult(chanceToHit):
    """
    Calculate turret damage multiplier from chance to hit.
    
    Based on EVE formula:
    https://wiki.eveuniversity.org/Turret_mechanics#Damage
    
    Includes wrecking hit calculation for proper damage distribution.
    
    Note: This can return values > 1.0 at high CTH because wrecking hits
    deal 3x damage. At CTH=1.0, expected damage mult is ~1.015.
    
    Args:
        chanceToHit: Chance to hit (0-1)
    
    Returns:
        Expected damage multiplier
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


# =============================================================================
# Turret Base Stats Extraction
# =============================================================================

def getTurretBaseStats(mod):
    """
    Get turret stats with ship/skill bonuses but WITHOUT charge modifiers.
    
    When a charge is loaded, getModifiedItemAttr returns values that already
    include the charge's range/falloff/tracking multipliers. We need to undo
    those effects to get the true base turret stats (with only ship/skill bonuses).
    
    Args:
        mod: The turret module
    
    Returns:
        Dict with: optimal, falloff, tracking, optimalSigRadius, damageMultiplier
    """
    # Get the modified values (includes charge effects if charge is loaded)
    optimal = mod.getModifiedItemAttr('maxRange') or 0
    falloff = mod.getModifiedItemAttr('falloff') or 0
    tracking = mod.getModifiedItemAttr('trackingSpeed') or 0
    optimalSigRadius = mod.getModifiedItemAttr('optimalSigRadius') or 0
    damageMult = mod.getModifiedItemAttr('damageMultiplier') or 1
    
    # If a charge is loaded, undo its range/falloff/tracking multiplier effects
    # Charges multiply these stats, so we divide them out to get base stats
    if mod.charge:
        chargeRangeMult = mod.charge.getAttribute('weaponRangeMultiplier') or 1
        chargeFalloffMult = mod.charge.getAttribute('fallofMultiplier') or 1  # EVE typo
        chargeTrackingMult = mod.charge.getAttribute('trackingSpeedMultiplier') or 1
        
        if chargeRangeMult != 0:
            optimal = optimal / chargeRangeMult
        if chargeFalloffMult != 0:
            falloff = falloff / chargeFalloffMult
        if chargeTrackingMult != 0:
            tracking = tracking / chargeTrackingMult
    
    return {
        'optimal': optimal,
        'falloff': falloff,
        'tracking': tracking,
        'optimalSigRadius': optimalSigRadius,
        'damageMultiplier': damageMult
    }


# =============================================================================
# Skill Multiplier
# =============================================================================

def getSkillMultiplier(mod):
    """
    Get the skill-based damage multiplier for a turret.
    
    Compares damage with current charge vs base charge damage to extract
    the multiplier from skills/ship bonuses.
    
    Args:
        mod: The turret module with a charge loaded
    
    Returns:
        Skill damage multiplier (float)
    """
    charge = mod.charge
    if not charge:
        return 1.0
    
    baseDamage = (
        (charge.getAttribute('emDamage') or 0) +
        (charge.getAttribute('thermalDamage') or 0) +
        (charge.getAttribute('kineticDamage') or 0) +
        (charge.getAttribute('explosiveDamage') or 0)
    )
    
    if baseDamage <= 0:
        return 1.0
    
    modifiedDamage = (
        (mod.getModifiedChargeAttr('emDamage') or 0) +
        (mod.getModifiedChargeAttr('thermalDamage') or 0) +
        (mod.getModifiedChargeAttr('kineticDamage') or 0) +
        (mod.getModifiedChargeAttr('explosiveDamage') or 0)
    )
    
    return modifiedDamage / baseDamage if baseDamage > 0 else 1.0


# =============================================================================
# Applied Volley Calculation
# =============================================================================

def calculateAppliedVolley(chargeData, distance, turretBase, trackingParams):
    """
    Calculate applied volley for a charge at a distance.
    
    Applies both range factor and tracking factor.
    
    Args:
        chargeData: Charge data dict with effective_optimal, effective_falloff, 
                    effective_tracking, raw_volley
        distance: Surface-to-surface distance (m)
        turretBase: Base turret stats dict
        trackingParams: Dict with atkSpeed, atkAngle, atkRadius, tgtSpeed, 
                        tgtAngle, tgtRadius, tgtSigRadius. None = perfect tracking.
    
    Returns:
        Applied volley (damage per shot accounting for range and tracking)
    """
    # Range factor
    if distance <= chargeData['effective_optimal']:
        rangeFactor = 1.0
    else:
        rangeFactor = calculateRangeFactor(
            chargeData['effective_optimal'],
            chargeData['effective_falloff'],
            distance,
            restrictedRange=False
        )
    
    # Tracking factor
    if trackingParams is None:
        trackingFactor = 1.0
    else:
        angularSpeed = calcAngularSpeed(
            trackingParams['atkSpeed'],
            trackingParams['atkAngle'],
            trackingParams['atkRadius'],
            distance,
            trackingParams['tgtSpeed'],
            trackingParams['tgtAngle'],
            trackingParams['tgtRadius']
        )
        trackingFactor = calcTrackingFactor(
            chargeData['effective_tracking'],
            turretBase['optimalSigRadius'],
            angularSpeed,
            trackingParams['tgtSigRadius']
        )
    
    # Chance to hit and damage multiplier
    cth = rangeFactor * trackingFactor
    damageMult = calcTurretDamageMult(cth)
    
    return chargeData['raw_volley'] * damageMult

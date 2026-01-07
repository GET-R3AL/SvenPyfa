import math

from eos.calc import calculateRangeFactor



def calcAngularSpeed(atkSpeed, atkAngle, atkRadius, distance, tgtSpeed, tgtAngle, tgtRadius):
    if distance is None:
        return 0
    
    atkAngleRad = atkAngle * math.pi / 180
    tgtAngleRad = tgtAngle * math.pi / 180
    
    ctcDistance = atkRadius + distance + tgtRadius
    transSpeed = abs(atkSpeed * math.sin(atkAngleRad) - tgtSpeed * math.sin(tgtAngleRad))
    
    if ctcDistance == 0:
        return 0 if transSpeed == 0 else math.inf
    else:
        return transSpeed / ctcDistance



def calcTrackingFactor(tracking, optimalSigRadius, angularSpeed, tgtSigRadius):
    if tracking <= 0 or tgtSigRadius <= 0:
        return 0
    if angularSpeed <= 0:
        return 1.0
    
    exponent = (angularSpeed * optimalSigRadius) / (tracking * tgtSigRadius)
    return 0.5 ** (exponent ** 2)



def calcTurretDamageMult(chanceToHit):
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



def getTurretBaseStats(mod):
    optimal = mod.getModifiedItemAttr('maxRange') or 0
    falloff = mod.getModifiedItemAttr('falloff') or 0
    tracking = mod.getModifiedItemAttr('trackingSpeed') or 0
    optimalSigRadius = mod.getModifiedItemAttr('optimalSigRadius') or 0
    damageMult = mod.getModifiedItemAttr('damageMultiplier') or 1
    
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



def getSkillMultiplier(mod):
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



def calculateAppliedVolley(chargeData, distance, turretBase, trackingParams):
    if distance <= chargeData['effective_optimal']:
        rangeFactor = 1.0
    else:
        rangeFactor = calculateRangeFactor(
            chargeData['effective_optimal'],
            chargeData['effective_falloff'],
            distance,
            restrictedRange=False
        )
    
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
    
    cth = rangeFactor * trackingFactor
    damageMult = calcTurretDamageMult(cth)
    
    return chargeData['raw_volley'] * damageMult

import math
from bisect import bisect_right

from logbook import Logger

from .projected import getProjectedParamsAtDistance


pyfalog = Logger(__name__)



def calcMissileFactor(atkEr, atkEv, atkDrf, tgtSpeed, tgtSigRadius):
    factors = [1]
    if atkEr > 0:
        factors.append(tgtSigRadius / atkEr)
    if tgtSpeed > 0 and atkEr > 0:
        factors.append(((atkEv * tgtSigRadius) / (atkEr * tgtSpeed)) ** atkDrf)
    return min(factors)



def _extractMultiplier(mod, attr):
    base = mod.getChargeBaseAttrValue(attr) or 0
    
    if base > 0:
        modified = mod.getModifiedChargeAttr(attr) or 0
        pyfalog.debug(f"DEBUG: _extractMultiplier({attr}): base={base}, modified={modified}, mult={modified/base}")
        return modified / base
    
    # Base is 0, we need to trick the eos logic to give us the multiplier
    # We use preAssign to set the base value to 1.0 for this calculation
    pyfalog.debug(f"DEBUG: _extractMultiplier({attr}): base is 0, attempting injection")
    mod.chargeModifiedAttributes.preAssign(attr, 1.0)
    try:
        # Get the modified value, which should now be 1.0 * multiplier
        multiplier = mod.getModifiedChargeAttr(attr) or 1.0
        pyfalog.debug(f"DEBUG: _extractMultiplier({attr}): injected base 1.0, got modified={multiplier}")
    finally:
        # Cleanup: remove the preAssign
        # Accessing private members is naughty but eos doesn't give us a clean way to remove preAssigns
        # and we must clean up to avoid side effects
        if attr in mod.chargeModifiedAttributes._ModifiedAttributeDict__preAssigns:
            del mod.chargeModifiedAttributes._ModifiedAttributeDict__preAssigns[attr]
            # Force recalculation by removing from cache
            if attr in mod.chargeModifiedAttributes._ModifiedAttributeDict__modified:
                del mod.chargeModifiedAttributes._ModifiedAttributeDict__modified[attr]
            if attr in mod.chargeModifiedAttributes._ModifiedAttributeDict__intermediary:
                del mod.chargeModifiedAttributes._ModifiedAttributeDict__intermediary[attr]
    
    return multiplier

def getDamageMultipliers(mod):
    if mod.charge is None:
        return {
            'emDamage': 1.0,
            'thermalDamage': 1.0,
            'kineticDamage': 1.0,
            'explosiveDamage': 1.0
        }
    
    multipliers = {}
    for dmgType in ('emDamage', 'thermalDamage', 'kineticDamage', 'explosiveDamage'):
        multipliers[dmgType] = _extractMultiplier(mod, dmgType)
    
    return multipliers


def getFlightMultipliers(mod):
    if mod.charge is None:
        return {'maxVelocity': 1.0, 'explosionDelay': 1.0}
    
    multipliers = {}
    for attr in ('maxVelocity', 'explosionDelay'):
        multipliers[attr] = _extractMultiplier(mod, attr)
    
    return multipliers


def getApplicationMultipliers(mod):
    if mod.charge is None:
        return {'aoeCloudSize': 1.0, 'aoeVelocity': 1.0, 'aoeDamageReductionFactor': 1.0}
    
    multipliers = {}
    for attr in ('aoeCloudSize', 'aoeVelocity', 'aoeDamageReductionFactor'):
        multipliers[attr] = _extractMultiplier(mod, attr)
    
    return multipliers


def getAllMultipliers(mod):
    # pyfalog.debug(f"DEBUG: getAllMultipliers called for {mod.item.name}, charge={mod.charge}")
    return (
        getDamageMultipliers(mod),
        getFlightMultipliers(mod),
        getApplicationMultipliers(mod)
    )



def calculateMissileRange(maxVelocity, mass, agility, flightTime):
    accelTime = min(flightTime, mass * agility / 1000000)
    duringAcceleration = maxVelocity / 2 * accelTime
    # Distance at full speed
    fullSpeed = maxVelocity * (flightTime - accelTime)
    return duringAcceleration + fullSpeed


def getMissileRangeData(charge, shipRadius, damageMults=None, flightMults=None, appMults=None):
    if flightMults is None:
        flightMults = {'maxVelocity': 1.0, 'explosionDelay': 1.0}
    if appMults is None:
        appMults = {'aoeCloudSize': 1.0, 'aoeVelocity': 1.0, 'aoeDamageReductionFactor': 1.0}
    if damageMults is None:
        damageMults = {'emDamage': 1.0, 'thermalDamage': 1.0, 'kineticDamage': 1.0, 'explosiveDamage': 1.0}
    
    baseVelocity = charge.getAttribute('maxVelocity') or 0
    baseExplosionDelay = charge.getAttribute('explosionDelay') or 0
    baseMass = charge.getAttribute('mass') or 1
    baseAgility = charge.getAttribute('agility') or 1
    
    if baseVelocity <= 0 or baseExplosionDelay <= 0:
        return None
    
    maxVelocity = baseVelocity * flightMults['maxVelocity']
    explosionDelay = baseExplosionDelay * flightMults['explosionDelay']
    
    # Calculate flight time (includes ship radius bonus)
    # Flight time has bonus based on ship radius: https://github.com/pyfa-org/Pyfa/issues/2083
    flightTime = explosionDelay / 1000 + shipRadius / maxVelocity
    
    # Discrete flight time: floor and ceil
    lowerTime = math.floor(flightTime)
    higherTime = math.ceil(flightTime)
    higherTime = math.ceil(flightTime)
    higherChance = flightTime - lowerTime
    
    lowerRange = calculateMissileRange(maxVelocity, baseMass, baseAgility, lowerTime)
    higherRange = calculateMissileRange(maxVelocity, baseMass, baseAgility, higherTime)
    
    # Make range center-to-surface (missiles spawn at ship center)
    lowerRange = max(0, lowerRange - shipRadius)
    higherRange = max(0, higherRange - shipRadius)
    
    maxEffectiveRange = higherRange
    
    # Get application stats with multipliers
    baseEr = charge.getAttribute('aoeCloudSize') or 0
    baseEv = charge.getAttribute('aoeVelocity') or 0
    baseDrf = charge.getAttribute('aoeDamageReductionFactor') or 1
    
    explosionRadius = baseEr * appMults['aoeCloudSize']
    explosionVelocity = baseEv * appMults['aoeVelocity']
    damageReductionFactor = baseDrf * appMults['aoeDamageReductionFactor']
    
    baseEm = charge.getAttribute('emDamage') or 0
    baseThermal = charge.getAttribute('thermalDamage') or 0
    baseKinetic = charge.getAttribute('kineticDamage') or 0
    baseExplosive = charge.getAttribute('explosiveDamage') or 0
    
    em = baseEm * damageMults['emDamage']
    thermal = baseThermal * damageMults['thermalDamage']
    kinetic = baseKinetic * damageMults['kineticDamage']
    explosive = baseExplosive * damageMults['explosiveDamage']
    totalDamage = em + thermal + kinetic + explosive
    
    return {
        'lowerRange': lowerRange,
        'higherRange': higherRange,
        'higherChance': higherChance,
        'maxEffectiveRange': maxEffectiveRange,
        'explosionRadius': explosionRadius,
        'explosionVelocity': explosionVelocity,
        'damageReductionFactor': damageReductionFactor,
        'totalDamage': totalDamage,
        'emDamage': em,
        'thermalDamage': thermal,
        'kineticDamage': kinetic,
        'explosiveDamage': explosive
    }



# Damage type priority for tie-breaking (EM > Thermal > Kinetic > Explosive)
DAMAGE_TYPE_PRIORITY = {
    'em': 0,
    'thermal': 1,
    'kinetic': 2,
    'explosive': 3
}


def getDominantDamageType(chargeName):
    nameLower = chargeName.lower()
    if 'mjolnir' in nameLower:
        return 'em'
    elif 'inferno' in nameLower:
        return 'thermal'
    elif 'scourge' in nameLower:
        return 'kinetic'
    elif 'nova' in nameLower:
        return 'explosive'
    return 'unknown'


def precomputeMissileChargeData(mod, charges, cycleTimeMs, shipRadius,
                                 damageMults=None, flightMults=None, appMults=None,
                                 tgtResists=None):
    if damageMults is None:
        damageMults = {'emDamage': 1.0, 'thermalDamage': 1.0, 'kineticDamage': 1.0, 'explosiveDamage': 1.0}
    if flightMults is None:
        flightMults = {'maxVelocity': 1.0, 'explosionDelay': 1.0}
    if appMults is None:
        appMults = {'aoeCloudSize': 1.0, 'aoeVelocity': 1.0, 'aoeDamageReductionFactor': 1.0}
    
    # Get launcher damage multiplier
    launcherDamageMult = mod.getModifiedItemAttr('damageMultiplier') or 1
    
    chargeData = []
    for charge in charges:
        rangeData = getMissileRangeData(charge, shipRadius, damageMults, flightMults, appMults)
        if rangeData is None:
            continue
        
        totalDamage = rangeData['totalDamage']
        if tgtResists:
            emRes, thermRes, kinRes, exploRes = tgtResists
            totalDamage = (
                rangeData['emDamage'] * (1 - emRes) +
                rangeData['thermalDamage'] * (1 - thermRes) +
                rangeData['kineticDamage'] * (1 - kinRes) +
                rangeData['explosiveDamage'] * (1 - exploRes)
            )
        
        rawVolley = totalDamage * launcherDamageMult
        rawDps = rawVolley / (cycleTimeMs / 1000) if cycleTimeMs > 0 else 0
        
        damageType = getDominantDamageType(charge.name)
        damagePriority = DAMAGE_TYPE_PRIORITY.get(damageType, 99)
        
        chargeData.append({
            'name': charge.name,
            'raw_volley': rawVolley,
            'raw_dps': rawDps,
            'lowerRange': rangeData['lowerRange'],
            'higherRange': rangeData['higherRange'],
            'higherChance': rangeData['higherChance'],
            'maxEffectiveRange': rangeData['maxEffectiveRange'],
            'explosionRadius': rangeData['explosionRadius'],
            'explosionVelocity': rangeData['explosionVelocity'],
            'damageReductionFactor': rangeData['damageReductionFactor'],
            'damage_priority': damagePriority
        })
    
    # Sort by maxEffectiveRange descending (longest range first for max range calculation)
    # Then by raw_dps descending for tie-breaking
    chargeData.sort(key=lambda x: (-x['maxEffectiveRange'], -x['raw_dps']))
    
    return chargeData


def getMaxEffectiveRange(chargeData):
    if not chargeData:
        return 0
    # Charge data is sorted by maxEffectiveRange descending
    return chargeData[0]['maxEffectiveRange']



def calculateRangeFactor(distance, lowerRange, higherRange, higherChance):
    if distance <= lowerRange:
        return 1.0
    elif distance <= higherRange:
        return higherChance
    else:
        return 0.0


def calculateAppliedVolley(chargeData, distance, tgtSpeed, tgtSigRadius):
    rangeFactor = calculateRangeFactor(
        distance,
        chargeData['lowerRange'],
        chargeData['higherRange'],
        chargeData['higherChance']
    )
    
    if rangeFactor == 0:
        return 0
    
    appFactor = calcMissileFactor(
        chargeData['explosionRadius'],
        chargeData['explosionVelocity'],
        chargeData['damageReductionFactor'],
        tgtSpeed,
        tgtSigRadius
    )
    
    return chargeData['raw_volley'] * rangeFactor * appFactor


def volleyToDps(volley, cycleTimeMs):
    if cycleTimeMs <= 0:
        return 0
    return volley / (cycleTimeMs / 1000)



def findBestCharge(chargeData, distance, tgtSpeed, tgtSigRadius):
    bestVolley = 0
    bestName = None
    bestIndex = 0
    bestPriority = 99
    
    for i, cd in enumerate(chargeData):
        volley = calculateAppliedVolley(cd, distance, tgtSpeed, tgtSigRadius)
        
        if volley > bestVolley or (volley == bestVolley and volley > 0 and cd['damage_priority'] < bestPriority):
            bestVolley = volley
            bestName = cd['name']
            bestIndex = i
            bestPriority = cd['damage_priority']
    
    return bestVolley, bestName, bestIndex



def _updateParamsWithCache(baseTgtSpeed, baseTgtSigRadius, projectedCache, distance):
    projected = getProjectedParamsAtDistance(projectedCache, distance)
    return projected['tgtSpeed'], projected['tgtSigRadius']


def calculateTransitions(chargeData, baseTgtSpeed, baseTgtSigRadius,
                         projectedCache, maxDistance=300000, resolution=1000):
    if not chargeData:
        return []
    
    pyfalog.debug(f"[MISSILE] Starting transition calculation with {len(chargeData)} charges")
    pyfalog.debug(f"[MISSILE] Base params: tgtSpeed={baseTgtSpeed}, tgtSig={baseTgtSigRadius}")
    
    transitions = []
    currentCharge = None
    
    # Start at distance 0
    tgtSpeed, tgtSigRadius = _updateParamsWithCache(baseTgtSpeed, baseTgtSigRadius, projectedCache, 0)
    bestVolley, bestName, bestIdx = findBestCharge(chargeData, 0, tgtSpeed, tgtSigRadius)
    transitions.append((0, bestIdx, bestName, bestVolley))
    currentCharge = bestName
    
    # Scan for transitions
    distance = resolution
    while distance <= maxDistance:
        tgtSpeed, tgtSigRadius = _updateParamsWithCache(baseTgtSpeed, baseTgtSigRadius, projectedCache, distance)
        bestVolley, bestName, bestIdx = findBestCharge(chargeData, distance, tgtSpeed, tgtSigRadius)
        
        if bestName != currentCharge:
        if bestName != currentCharge:
            low, high = distance - resolution, distance
            while high - low > 10:
                mid = (low + high) // 2
                midSpeed, midSig = _updateParamsWithCache(baseTgtSpeed, baseTgtSigRadius, projectedCache, mid)
                _, midName, _ = findBestCharge(chargeData, mid, midSpeed, midSig)
                if midName == currentCharge:
                    low = mid
                else:
                    high = mid
            
            # Get volley at transition
            highSpeed, highSig = _updateParamsWithCache(baseTgtSpeed, baseTgtSigRadius, projectedCache, high)
            bestVolley, _, _ = findBestCharge(chargeData, high, highSpeed, highSig)
            
            transitions.append((high, bestIdx, bestName, bestVolley))
            pyfalog.debug(f"[MISSILE] Transition @ {high/1000:.1f}km: {currentCharge} -> {bestName}")
            currentCharge = bestName
        
        # Stop if we're past all missile ranges
        if bestVolley < 0.01:
            transitions.append((distance, -1, None, 0))
            break
        
        distance += resolution
    
    pyfalog.debug(f"[MISSILE] Completed: {len(transitions)} transition points found")
    
    return transitions



def getVolleyAtDistance(transitions, chargeData, distance,
                        baseTgtSpeed, baseTgtSigRadius, projectedCache):
    if not transitions or not chargeData:
        return 0, None
    
    # Find which charge is optimal at this distance
    distances = [t[0] for t in transitions]
    idx = bisect_right(distances, distance) - 1
    if idx < 0:
        idx = 0
    
    chargeIdx = transitions[idx][1]
    if chargeIdx < 0 or chargeIdx >= len(chargeData):
        return 0, None
    
    cd = chargeData[chargeIdx]
    
    cd = chargeData[chargeIdx]
    
    tgtSpeed, tgtSigRadius = _updateParamsWithCache(baseTgtSpeed, baseTgtSigRadius, projectedCache, distance)
    volley = calculateAppliedVolley(cd, distance, tgtSpeed, tgtSigRadius)
    
    return volley, cd['name']


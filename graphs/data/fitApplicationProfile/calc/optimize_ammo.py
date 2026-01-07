from bisect import bisect_right

from logbook import Logger

from .turret import calculateAppliedVolley
from .projected import getProjectedParamsAtDistance


pyfalog = Logger(__name__)



def volleyToDps(volley, cycleTimeMs):
    if cycleTimeMs <= 0:
        return 0
    return volley / (cycleTimeMs / 1000)



def findBestCharge(chargeData, distance, turretBase, trackingParams):
    bestVolley = 0
    bestName = None
    bestIndex = 0
    
    for i, cd in enumerate(chargeData):
        volley = calculateAppliedVolley(cd, distance, turretBase, trackingParams)
        if volley > bestVolley:
            bestVolley = volley
            bestName = cd['name']
            bestIndex = i
    
    return bestVolley, bestName, bestIndex



def _updateTrackingWithCache(baseTrackingParams, projectedCache, distance):
    if baseTrackingParams is None:
        return None
    
    params = baseTrackingParams.copy()
    projected = getProjectedParamsAtDistance(projectedCache, distance)
    params['tgtSpeed'] = projected['tgtSpeed']
    params['tgtSigRadius'] = projected['tgtSigRadius']
    return params


def calculateTransitions(chargeData, turretBase, baseTrackingParams,
                         projectedCache,
                         maxDistance=300000, resolution=1000):
    if not chargeData:
        pyfalog.debug("[AMMO] calculateTransitions: no chargeData")
        return []
    
    pyfalog.debug(f"[AMMO] Starting transition calculation with {len(chargeData)} charges, "
                  f"resolution={resolution}m, max={maxDistance/1000:.0f}km")
    if baseTrackingParams:
        pyfalog.debug(f"[AMMO] Base params: tgtSpeed={baseTrackingParams.get('tgtSpeed', 0):.0f}, "
                      f"tgtSig={baseTrackingParams.get('tgtSigRadius', 0):.0f}")
        pyfalog.debug(f"[AMMO] Using projected cache: {projectedCache.get('hasProjected', False)}")
    
    transitions = []
    currentCharge = None
    
    params0 = _updateTrackingWithCache(baseTrackingParams, projectedCache, 0)
    bestVolley, bestName, bestIdx = findBestCharge(chargeData, 0, turretBase, params0)
    transitions.append((0, bestIdx, bestName, bestVolley))
    currentCharge = bestName
    
    distance = resolution
    while distance <= maxDistance:
        params = _updateTrackingWithCache(baseTrackingParams, projectedCache, distance)
        bestVolley, bestName, bestIdx = findBestCharge(chargeData, distance, turretBase, params)
        
        if bestName != currentCharge:
        if bestName != currentCharge:
            low, high = distance - resolution, distance
            while high - low > 10:
                mid = (low + high) // 2
                paramsMid = _updateTrackingWithCache(baseTrackingParams, projectedCache, mid)
                _, midName, _ = findBestCharge(chargeData, mid, turretBase, paramsMid)
                if midName == currentCharge:
                    low = mid
                else:
                    high = mid
            
            paramsHigh = _updateTrackingWithCache(baseTrackingParams, projectedCache, high)
            bestVolley, _, _ = findBestCharge(chargeData, high, turretBase, paramsHigh)
            
            transitions.append((high, bestIdx, bestName, bestVolley))
            pyfalog.debug(f"[AMMO] Transition @ {high/1000:.1f}km: {currentCharge} -> {bestName}")
            currentCharge = bestName
        
        distance += resolution
    
    pyfalog.debug(f"[AMMO] Completed: {len(transitions)} transition points found")
    for t in transitions:
        pyfalog.debug(f"[AMMO]   {t[0]/1000:.1f}km: {t[2]}")
    
    return transitions



def getVolleyAtDistance(transitions, chargeData, turretBase, distance,
                        baseTrackingParams, projectedCache):
    if not transitions:
        return 0, None
    
    distances = [t[0] for t in transitions]
    idx = bisect_right(distances, distance) - 1
    if idx < 0:
        idx = 0
    
    chargeIdx = transitions[idx][1]
    cd = chargeData[chargeIdx]
    
    params = _updateTrackingWithCache(baseTrackingParams, projectedCache, distance)
    volley = calculateAppliedVolley(cd, distance, turretBase, params)
    
    return volley, cd['name']

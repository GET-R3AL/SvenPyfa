import math
from bisect import bisect_right

from eos.calc import calculateRangeFactor
from eos.utils.float import floatUnerr
from graphs.calc import checkLockRange, checkDroneControlRange
from service.const import GraphDpsDroneMode
from service.settings import GraphSettings

from logbook import Logger

pyfalog = Logger(__name__)



from graphs.data.fitDamageStats.calc.projected import (
    getScramRange,
    getScrammables,
    getTackledSpeed,
    getSigRadiusMult,
)



def buildProjectedCache(src, tgt, commonData, baseTgtSpeed, baseTgtSigRadius,
                        maxDistance=300000, resolution=1000, existingCache=None):
    applyProjected = commonData.get('applyProjected', False)
    
    if not applyProjected:
        return {
            'distances': [],
            'cache': {},
            'hasProjected': False,
            'baseTgtSpeed': baseTgtSpeed,
            'baseTgtSigRadius': baseTgtSigRadius,
            'maxCachedDistance': 0
        }
    
    # Check if we can extend an existing cache: only needing base parameter match
    canExtend = (
        existingCache is not None and
        existingCache.get('hasProjected', False) and
        existingCache.get('baseTgtSpeed') == baseTgtSpeed and
        existingCache.get('baseTgtSigRadius') == baseTgtSigRadius
    )
    
    if canExtend:
        existingMax = existingCache.get('maxCachedDistance', 0)
        
        if existingMax >= maxDistance:
            pyfalog.debug(f"[PROJECTED] Existing cache sufficient: {existingMax/1000:.0f}km >= {maxDistance/1000:.0f}km needed")
            return existingCache
        
        sigStr = 'inf' if baseTgtSigRadius == float('inf') else f"{baseTgtSigRadius:.1f}m"
        pyfalog.debug(f"[PROJECTED] Extending cache: {existingMax/1000:.0f}km -> {maxDistance/1000:.0f}km (baseSig={sigStr})")
        distances = existingCache['distances'].copy()
        cache = existingCache['cache'].copy()
        startDistance = existingMax + resolution
    else:
        sigStr = 'inf' if baseTgtSigRadius == float('inf') else f"{baseTgtSigRadius:.1f}m"
        pyfalog.debug(f"[PROJECTED] Building new cache: 0-{maxDistance/1000:.0f}km @ {resolution/1000:.1f}km intervals (baseSig={sigStr})")
        distances = []
        cache = {}
        startDistance = 0
    
    srcScramRange = commonData.get('srcScramRange', 0)
    tgtScrammables = commonData.get('tgtScrammables', ())
    webMods = commonData.get('webMods', ())
    webDrones = commonData.get('webDrones', ())
    webFighters = commonData.get('webFighters', ())
    tpMods = commonData.get('tpMods', ())
    tpDrones = commonData.get('tpDrones', ())
    tpFighters = commonData.get('tpFighters', ())
    
    if webMods or webDrones or webFighters:
        pyfalog.debug(f"[PROJECTED] Webs: {len(webMods)} mods, {len(webDrones)} drones, {len(webFighters)} fighters")
    if tpMods or tpDrones or tpFighters:
        pyfalog.debug(f"[PROJECTED] TPs: {len(tpMods)} mods, {len(tpDrones)} drones, {len(tpFighters)} fighters")
    
    distance = startDistance
    entriesAdded = 0
    prevSpeed = None
    while distance <= maxDistance:
        tackledSpeed = getTackledSpeed(
            src=src,
            tgt=tgt,
            currentUntackledSpeed=baseTgtSpeed,
            srcScramRange=srcScramRange,
            tgtScrammables=tgtScrammables,
            webMods=webMods,
            webDrones=webDrones,
            webFighters=webFighters,
            distance=distance
        )
        
        sigMult = getSigRadiusMult(
            src=src,
            tgt=tgt,
            tgtSpeed=tackledSpeed,
            srcScramRange=srcScramRange,
            tgtScrammables=tgtScrammables,
            tpMods=tpMods,
            tpDrones=tpDrones,
            tpFighters=tpFighters,
            distance=distance
        )
        
        if prevSpeed is not None and abs(tackledSpeed - prevSpeed) > baseTgtSpeed * 0.05:
            pyfalog.debug(f"[PROJECTED] Speed change @ {distance/1000:.1f}km: {prevSpeed:.0f} -> {tackledSpeed:.0f} m/s")
        prevSpeed = tackledSpeed
        
        distances.append(distance)
        cache[distance] = {
            'tgtSpeed': tackledSpeed,
            'tgtSigRadius': baseTgtSigRadius * sigMult
        }
        
        distance += resolution
        entriesAdded += 1
    
    distances.sort()
    
    return {
        'distances': distances,
        'cache': cache,
        'hasProjected': True,
        'baseTgtSpeed': baseTgtSpeed,
        'baseTgtSigRadius': baseTgtSigRadius,
        'maxCachedDistance': distances[-1] if distances else 0
    }


def getProjectedParamsAtDistance(projectedCache, distance, interpolate=True):
        return {
            'tgtSpeed': projectedCache.get('baseTgtSpeed', 0),
            'tgtSigRadius': projectedCache.get('baseTgtSigRadius', 0)
        }
    
    distances = projectedCache.get('distances', [])
    cache = projectedCache.get('cache', {})
    
    if not distances:
        return {
            'tgtSpeed': projectedCache.get('baseTgtSpeed', 0),
            'tgtSigRadius': projectedCache.get('baseTgtSigRadius', 0)
        }
    
    distLow = distances[idx]
    distHigh = distances[idx + 1]
    
    if not interpolate or distance <= distLow:
        return cache[distLow]
    
    tgtSpeed = cacheLow['tgtSpeed'] + t * (cacheHigh['tgtSpeed'] - cacheLow['tgtSpeed'])
    if cacheLow['tgtSigRadius'] == float('inf') or cacheHigh['tgtSigRadius'] == float('inf'):
        tgtSigRadius = float('inf')
    else:
        tgtSigRadius = cacheLow['tgtSigRadius'] + t * (cacheHigh['tgtSigRadius'] - cacheLow['tgtSigRadius'])
    
    return {
        'tgtSpeed': tgtSpeed,
        'tgtSigRadius': tgtSigRadius
    }

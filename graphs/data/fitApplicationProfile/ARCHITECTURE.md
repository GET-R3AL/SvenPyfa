# Ammo Optimal DPS Graph - Architecture

## Implementation Status: ✅ COMPLETE

The distance-keyed projected effects cache has been implemented with dynamic range
calculation and cache reuse across fit changes.

## Data Flow

```
1. _getCommonData() [getter.py]
   │
   ├─ PHASE 1: Gather turret stats and determine max effective range
   │   └─ getTurretRangeInfo() per turret type
   │       ├─ getTurretBaseStats()
   │       ├─ filterChargesByQuality()
   │       └─ getMaxEffectiveRange() = optimal*longestRangeMult + falloff*3.1
   │
   ├─ PHASE 2: Build/extend projected cache to max effective range
   │   └─ buildProjectedCache(maxDistance=maxEffectiveRange, existingCache=...)
   │       └─ Reuses existing cache if target unchanged (extends if needed)
   │       └─ {distance: {tgtSpeed, tgtSigRadius}}
   │
   └─ PHASE 3: Build turret cache with transitions
       └─ buildTurretCacheEntry() per turret type
           ├─ filterProjectileByBand() (if resists)
           ├─ precomputeChargeData()
           └─ calculateTransitions(maxDistance=turretMaxEffectiveRange)

2. getVolleyAtDistance() [on cursor move]
   └─ bisect to find charge (O(log n))
   └─ _updateTrackingWithCache() - O(1) lookup from projectedCache
   └─ calculateAppliedVolley()
```

## Cache Keys and Reuse

### Turret Cache Key
```python
(id(src.item), qualityTier, tgtResists, applyProjected, tgtSpeed, tgtSigRadius)
```
Invalidated when: attacker fit changes, ammo quality changes, target resists change

### Projected Cache Key
```python
(id(src.item), applyProjected, tgtSpeed, tgtSigRadius)
```
**Includes source fit ID** because webs/TPs come from the source fit.
**Invalidated when**: Source fit changes (webs/grapples/TPs could be different)
**Cleared on**: `fitChanged`, `fitRemoved`, `graphSwitched` events

## Cache Invalidation

The `_clearInternalCache()` method in `graph.py` clears all ammo-related caches:
- `_ammo_turret_cache` - Turret data with transitions
- `_ammo_projected_cache` - Distance-keyed target speed/sig
- `_ammo_charge_cache` - Filtered charge lists per turret
- `_ammo_missile_cache` - Missile data (if applicable)
- `_ammo_analysis_done` - Analysis tracking set

## Interpolation for Smooth Curves

`getProjectedParamsAtDistance()` uses **linear interpolation** between cache entries.
This prevents "stair-step" artifacts when grapples/webs have falloff mechanics:

```python
# Instead of returning nearest cached value:
return cache[distances[idx]]

# We interpolate between cache[distances[idx]] and cache[distances[idx+1]]:
t = (distance - distLow) / (distHigh - distLow)
tgtSpeed = cacheLow['tgtSpeed'] + t * (cacheHigh['tgtSpeed'] - cacheLow['tgtSpeed'])
```

## Performance Characteristics

| Operation | Before | After |
|-----------|--------|-------|
| getTackledSpeed calls | 300+ per transition scan | N (once per cache entry, only to maxEffectiveRange) |
| getSigRadiusMult calls | 300+ per transition scan | N (once per cache entry, only to maxEffectiveRange) |
| Cursor move query | Full projected recalc | O(1) cache lookup with interpolation |
| Cache range | Fixed 300km | Dynamic (optimal + falloff*3.1) |
| Grapple curves | Stair-step at 1km intervals | Smooth via interpolation |

## Debug Logging

Enable `--debug` flag to see cache operations:
```
[AMMO] Projected effects: 1 webs, 0 TPs
[AMMO] Cache keys - turret: (123456, 'all')..., projected: (123456, True)...
[PROJECTED] Webs: 1 mods, 0 drones, 0 fighters
[PROJECTED] Building new cache: 0-25km @ 1.0km intervals
[PROJECTED] Speed change @ 5.0km: 500 -> 250 m/s
[PROJECTED] Cache complete: 26 entries (26 new)
```

## Key Functions

### calc/charges.py
```python
getLongestRangeMultiplier(charges)
    → Returns max rangeMultiplier from charge list
```

### calc/projected.py
```python
buildProjectedCache(src, tgt, commonData, baseTgtSpeed, baseTgtSigRadius,
                    maxDistance, resolution, existingCache=None)
    → Returns: {
        'distances': [0, 1000, 2000, ...],
        'cache': {distance: {'tgtSpeed': float, 'tgtSigRadius': float}},
        'hasProjected': bool,
        'maxCachedDistance': int,  # Highest cached distance
        'baseTgtSpeed': float,
        'baseTgtSigRadius': float
    }

getProjectedParamsAtDistance(projectedCache, distance, interpolate=True)
    → Returns interpolated {'tgtSpeed': float, 'tgtSigRadius': float}
```

### getter.py
```python
getMaxEffectiveRange(turretBase, charges)
    → optimal * longestRangeMult + falloff * 3.1

getTurretRangeInfo(mod, qualityTier, chargeCache)
    → {turret_base, charges, max_effective_range, cycle_time_ms}
```

## Files

### Core Modules
- `getter.py` - Orchestration, mixins, cache management, max range calculation
- `calc/__init__.py` - Package exports

### Calculation Modules
- `calc/turret.py` - Turret mechanics (tracking, angular speed, damage)
- `calc/charges.py` - Charge filtering, stats, resist application, `getLongestRangeMultiplier()`
- `calc/optimize_ammo.py` - Pareto filter, transitions (now with dynamic maxDistance)
- `calc/projected.py` - Distance-keyed projected effects cache with extension support
- `calc/launcher.py` - Missile support (stub)

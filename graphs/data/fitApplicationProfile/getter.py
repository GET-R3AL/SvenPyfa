from eos.const import FittingHardpoint
from logbook import Logger

from graphs.data.base.getter import SmoothPointGetter
from graphs.data.fitDamageStats.calc.projected import (
    getScramRange, getScrammables
)
from service.settings import GraphSettings
from .calc.valid_charges import getValidChargesForModule

from .calc.turret import (
    getTurretBaseStats,
    getSkillMultiplier
)
from .calc.charges import (
    filterChargesByQuality,
    filterProjectileByBand,
    precomputeChargeData,
    getLongestRangeMultiplier
)
from .calc.optimize_ammo import (
    volleyToDps,
    calculateTransitions,
    getVolleyAtDistance
)
from .calc.projected import (
    buildProjectedCache
)
from .calc.launcher import (
    getAllMultipliers as getLauncherMultipliers,
    precomputeMissileChargeData,
    getMaxEffectiveRange as getMissileMaxEffectiveRange,
    calculateTransitions as calculateMissileTransitions,
    getVolleyAtDistance as getMissileVolleyAtDistance,
    volleyToDps as missileVolleyToDps
)


pyfalog = Logger(__name__)



def getMaxEffectiveRange(turretBase, charges):
    longestRangeMult = getLongestRangeMultiplier(charges)
    effectiveOptimal = turretBase['optimal'] * longestRangeMult
    effectiveMaxRange = effectiveOptimal + turretBase['falloff'] * 3.1
    return int(effectiveMaxRange)


def getTurretRangeInfo(mod, qualityTier, chargeCache=None):
    turretBase = getTurretBaseStats(mod)
    
    cycleParams = mod.getCycleParameters()
    if cycleParams is None:
        return None
    cycleTimeMs = cycleParams.averageTime
    
    # Get and filter charges - use cache if available
    chargeCacheKey = (mod.item.ID, qualityTier)
    if chargeCache is not None and chargeCacheKey in chargeCache:
        charges = chargeCache[chargeCacheKey]
    else:
        allCharges = list(getValidChargesForModule(mod))
        charges = filterChargesByQuality(allCharges, qualityTier)
        if chargeCache is not None:
            chargeCache[chargeCacheKey] = charges
    
    if not charges:
        return None
    
    maxEffectiveRange = getMaxEffectiveRange(turretBase, charges)
    
    return {
        'turret_base': turretBase,
        'charges': charges,
        'max_effective_range': maxEffectiveRange,
        'cycle_time_ms': cycleTimeMs
    }



def getLauncherRangeInfo(mod, qualityTier, shipRadius, chargeCache=None):
    cycleParams = mod.getCycleParameters()
    if cycleParams is None:
        return None
    cycleTimeMs = cycleParams.averageTime
    
    # Get and filter charges - use cache if available
    chargeCacheKey = (mod.item.ID, qualityTier)
    if chargeCache is not None and chargeCacheKey in chargeCache:
        charges = chargeCache[chargeCacheKey]
    else:
        allCharges = list(getValidChargesForModule(mod))
        charges = filterChargesByQuality(allCharges, qualityTier)
        if chargeCache is not None:
            chargeCache[chargeCacheKey] = charges
    
    if not charges:
        return None
    
    damageMults, flightMults, appMults = getLauncherMultipliers(mod)
    
    launcherDamageMult = mod.getModifiedItemAttr('damageMultiplier') or 1
    
    chargeData = precomputeMissileChargeData(
        mod, charges, cycleTimeMs, shipRadius,
        damageMults, flightMults, appMults,
        tgtResists=None
    )
    
    if not chargeData:
        return None
    
    maxEffectiveRange = getMissileMaxEffectiveRange(chargeData)
    
    return {
        'charges': charges,
        'charge_data': chargeData,
        'max_effective_range': maxEffectiveRange,
        'cycle_time_ms': cycleTimeMs,
        'damage_mults': damageMults,
        'flight_mults': flightMults,
        'app_mults': appMults,
        'launcher_damage_mult': launcherDamageMult
    }



def countWeaponGroups(src):
    turretCount = 0
    launcherCount = 0
    
    for mod in src.item.activeModulesIter():
        if mod.getModifiedItemAttr('miningAmount'):
            continue
        
        if mod.hardpoint == FittingHardpoint.TURRET:
            turretCount += 1
        elif mod.hardpoint == FittingHardpoint.MISSILE:
            launcherCount += 1
    
    return turretCount, launcherCount


def getDominantWeaponType(src):
    turretCount, launcherCount = countWeaponGroups(src)
    
    if turretCount == 0 and launcherCount == 0:
        return None
    
    if turretCount >= launcherCount:
        return 'turret'
    else:
        return 'launcher'



def buildTurretCacheEntry(mod, qualityTier, tgtResists, baseTrackingParams,
                          projectedCache, chargeCache=None, rangeInfo=None):
    pyfalog.debug(f"[AMMO] buildTurretCacheEntry START for {mod.item.name}")
    
    if rangeInfo is not None:
        turretBase = rangeInfo['turret_base']
        charges = rangeInfo['charges']
        cycleTimeMs = rangeInfo['cycle_time_ms']
    else:
        turretBase = getTurretBaseStats(mod)
        cycleParams = mod.getCycleParameters()
        if cycleParams is None:
            return None
        cycleTimeMs = cycleParams.averageTime
        
        chargeCacheKey = (mod.item.ID, qualityTier)
        if chargeCache is not None and chargeCacheKey in chargeCache:
            charges = chargeCache[chargeCacheKey]
        else:
            allCharges = list(getValidChargesForModule(mod))
            charges = filterChargesByQuality(allCharges, qualityTier)
            if chargeCache is not None:
                chargeCache[chargeCacheKey] = charges
        
        if not charges:
            return None
    
    if tgtResists:
        charges = filterProjectileByBand(charges, tgtResists)
        pyfalog.debug(f"[AMMO] After projectile band filter: {len(charges)} charges")
    
    if not charges:
        return None
    
    skillMult = getSkillMultiplier(mod)
    
    chargeData = precomputeChargeData(turretBase, charges, skillMult, tgtResists)
    pyfalog.debug(f"[AMMO] Precomputed {len(chargeData)} charge data entries")
    
    maxEffectiveOptimal = max(cd['effective_optimal'] for cd in chargeData)
    maxEffectiveFalloff = max(cd['effective_falloff'] for cd in chargeData)
    maxEffectiveRange = int(maxEffectiveOptimal + maxEffectiveFalloff * 3.1)
    pyfalog.debug(f"[AMMO] Max effective range for this turret: {maxEffectiveRange/1000:.1f}km")
    
    transitions = calculateTransitions(
        chargeData, turretBase, baseTrackingParams,
        projectedCache,
        maxDistance=maxEffectiveRange
    )
    
    pyfalog.debug(f"[AMMO] buildTurretCacheEntry END for {mod.item.name}")
    
    return {
        'charge_data': chargeData,
        'transitions': transitions,
        'turret_base': turretBase,
        'cycle_time_ms': cycleTimeMs,
        'count': 1
    }


def buildLauncherCacheEntry(mod, qualityTier, tgtResists, shipRadius,
                            baseTgtSpeed, baseTgtSigRadius,
                            projectedCache, chargeCache=None, rangeInfo=None):
    pyfalog.debug(f"[AMMO] buildLauncherCacheEntry START for {mod.item.name}")
    
    if rangeInfo is not None:
        charges = rangeInfo['charges']
        cycleTimeMs = rangeInfo['cycle_time_ms']
        damageMults = rangeInfo['damage_mults']
        flightMults = rangeInfo['flight_mults']
        appMults = rangeInfo['app_mults']
    else:
        cycleParams = mod.getCycleParameters()
        if cycleParams is None:
            return None
        cycleTimeMs = cycleParams.averageTime
        
        chargeCacheKey = (mod.item.ID, qualityTier)
        if chargeCache is not None and chargeCacheKey in chargeCache:
            charges = chargeCache[chargeCacheKey]
        else:
            allCharges = list(getValidChargesForModule(mod))
            charges = filterChargesByQuality(allCharges, qualityTier)
            if chargeCache is not None:
                chargeCache[chargeCacheKey] = charges
        
        if not charges:
            return None
        
        damageMults, flightMults, appMults = getLauncherMultipliers(mod)
        
    chargeData = precomputeMissileChargeData(
        mod, charges, cycleTimeMs, shipRadius,
        damageMults, flightMults, appMults, tgtResists
    )
    
    if not chargeData:
        return None
    
    pyfalog.debug(f"[AMMO] Precomputed {len(chargeData)} missile charge data entries")
    
    maxEffectiveRange = getMissileMaxEffectiveRange(chargeData)
    pyfalog.debug(f"[AMMO] Max effective range for this launcher: {maxEffectiveRange/1000:.1f}km")
    
    transitions = calculateMissileTransitions(
        chargeData, baseTgtSpeed, baseTgtSigRadius,
        projectedCache,
        maxDistance=int(maxEffectiveRange)
    )
    
    pyfalog.debug(f"[AMMO] buildLauncherCacheEntry END for {mod.item.name}")
    
    return {
        'charge_data': chargeData,
        'transitions': transitions,
        'cycle_time_ms': cycleTimeMs,
        'count': 1
    }



class YOptimalAmmoDpsMixin:
    def _getOptimalDpsAtDistance(self, distance, weaponCache, trackingParams, projectedCache, weaponType):
        totalDps = 0

        if distance == 0:  # Log details at distance 0 for debugging
            pyfalog.debug(f"[DPS-CALC] weaponType={weaponType}, weaponCache has {len(weaponCache)} groups")
            pyfalog.debug(f"[DPS-CALC] trackingParams={trackingParams}")
            pyfalog.debug(f"[DPS-CALC] projectedCache has {len(projectedCache)} entries")

        if weaponType == 'turret':
            for group_id, groupInfo in weaponCache.items():
                if distance == 0:
                    pyfalog.debug(f"[DPS-CALC] Turret group {group_id}: {len(groupInfo.get('transitions', []))} transitions, {len(groupInfo.get('charge_data', []))} charges")
                volley, _ = getVolleyAtDistance(
                    groupInfo['transitions'],
                    groupInfo['charge_data'],
                    groupInfo['turret_base'],
                    distance,
                    trackingParams,
                    projectedCache
                )
                if distance == 0:
                    pyfalog.debug(f"[DPS-CALC] Turret volley at {distance}m = {volley}")
                dps = volleyToDps(volley, groupInfo['cycle_time_ms'])
                totalDps += dps * groupInfo['count']
        else:  # launcher
            for group_id, groupInfo in weaponCache.items():
                if distance == 0:
                    pyfalog.debug(f"[DPS-CALC] Launcher group {group_id}: {len(groupInfo.get('transitions', []))} transitions, {len(groupInfo.get('charge_data', []))} charges")
                volley, _ = getMissileVolleyAtDistance(
                    groupInfo['transitions'],
                    groupInfo['charge_data'],
                    distance,
                    trackingParams['tgtSpeed'],
                    trackingParams['tgtSigRadius'],
                    projectedCache
                )
                if distance == 0:
                    pyfalog.debug(f"[DPS-CALC] Launcher volley at {distance}m = {volley}")
                dps = missileVolleyToDps(volley, groupInfo['cycle_time_ms'])
                totalDps += dps * groupInfo['count']

        if distance == 0:
            pyfalog.debug(f"[DPS-CALC] Total DPS at {distance}m = {totalDps}")

        return totalDps

    def _getOptimalDpsWithAmmoAtDistance(self, distance, weaponCache, trackingParams, projectedCache, weaponType):
        totalDps = 0
        ammoName = None
        
        if weaponType == 'turret':
            for groupInfo in weaponCache.values():
                volley, name = getVolleyAtDistance(
                    groupInfo['transitions'],
                    groupInfo['charge_data'],
                    groupInfo['turret_base'],
                    distance,
                    trackingParams,
                    projectedCache
                )
                dps = volleyToDps(volley, groupInfo['cycle_time_ms'])
                totalDps += dps * groupInfo['count']
                if ammoName is None:
                    ammoName = name
        else:  # launcher
            for groupInfo in weaponCache.values():
                volley, name = getMissileVolleyAtDistance(
                    groupInfo['transitions'],
                    groupInfo['charge_data'],
                    distance,
                    trackingParams['tgtSpeed'],
                    trackingParams['tgtSigRadius'],
                    projectedCache
                )
                dps = missileVolleyToDps(volley, groupInfo['cycle_time_ms'])
                totalDps += dps * groupInfo['count']
                if ammoName is None:
                    ammoName = name
        
        return totalDps, ammoName


class YOptimalAmmoVolleyMixin:
    def _getOptimalVolleyAtDistance(self, distance, weaponCache, trackingParams, projectedCache, weaponType):
        totalVolley = 0
        
        if weaponType == 'turret':
            for groupInfo in weaponCache.values():
                volley, _ = getVolleyAtDistance(
                    groupInfo['transitions'],
                    groupInfo['charge_data'],
                    groupInfo['turret_base'],
                    distance,
                    trackingParams,
                    projectedCache
                )
                totalVolley += volley * groupInfo['count']
        else:  # launcher
            for groupInfo in weaponCache.values():
                volley, _ = getMissileVolleyAtDistance(
                    groupInfo['transitions'],
                    groupInfo['charge_data'],
                    distance,
                    trackingParams['tgtSpeed'],
                    trackingParams['tgtSigRadius'],
                    projectedCache
                )
                totalVolley += volley * groupInfo['count']
        
        return totalVolley

    def _getOptimalVolleyWithAmmoAtDistance(self, distance, weaponCache, trackingParams, projectedCache, weaponType):
        totalVolley = 0
        ammoName = None
        
        if weaponType == 'turret':
            for groupInfo in weaponCache.values():
                volley, name = getVolleyAtDistance(
                    groupInfo['transitions'],
                    groupInfo['charge_data'],
                    groupInfo['turret_base'],
                    distance,
                    trackingParams,
                    projectedCache
                )
                totalVolley += volley * groupInfo['count']
                if ammoName is None:
                    ammoName = name
        else:  # launcher
            for groupInfo in weaponCache.values():
                volley, name = getMissileVolleyAtDistance(
                    groupInfo['transitions'],
                    groupInfo['charge_data'],
                    distance,
                    trackingParams['tgtSpeed'],
                    trackingParams['tgtSigRadius'],
                    projectedCache
                )
                totalVolley += volley * groupInfo['count']
                if ammoName is None:
                    ammoName = name
        
        return totalVolley, ammoName



class XDistanceMixin(SmoothPointGetter):

    _baseResolution = 100

    def _getCommonData(self, miscParams, src, tgt):
        # Get settings
        qualityTier = getattr(self.graph, '_ammoQuality', 'all')
        ignoreResists = GraphSettings.getInstance().get('ammoOptimalIgnoreResists')
        applyProjected = GraphSettings.getInstance().get('ammoOptimalApplyProjected')

        tgtResists = None if (ignoreResists or tgt is None) else tgt.getResists()
        tgtSpeed = miscParams.get('tgtSpeed', 0) or 0
        tgtSigRadius = tgt.getSigRadius() if tgt else 0
        shipRadius = src.getRadius()

        weaponType = getDominantWeaponType(src)

        fit_id = src.item.ID

        atkSpeed = miscParams.get('atkSpeed', 0) or 0
        atkAngle = miscParams.get('atkAngle', 0) or 0
        tgtAngle = miscParams.get('tgtAngle', 0) or 0

        weaponCacheKey = (fit_id, weaponType, qualityTier, tgtResists, applyProjected, tgtSpeed, tgtSigRadius, atkSpeed, atkAngle, tgtAngle)

        projectedCacheKey = (fit_id, tgtSpeed, tgtSigRadius, atkSpeed, atkAngle, tgtAngle)
        
        if not hasattr(self.graph, '_ammo_weapon_cache'):
            self.graph._ammo_weapon_cache = {}
        if not hasattr(self.graph, '_ammo_charge_cache'):
            self.graph._ammo_charge_cache = {}
        if not hasattr(self.graph, '_ammo_projected_cache'):
            self.graph._ammo_projected_cache = {}
        
        commonData = {
            'applyProjected': applyProjected,
            'src_radius': shipRadius,
            'weapon_type': weaponType,
        }
        
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
        
        if weaponCacheKey in self.graph._ammo_weapon_cache:
            cached_weapon = self.graph._ammo_weapon_cache[weaponCacheKey]
            commonData['weapon_cache'] = cached_weapon
            commonData['projected_cache'] = self.graph._ammo_projected_cache.get(projectedCacheKey, {})
            return commonData
        
        if weaponType is None:
            commonData['weapon_cache'] = {}
            commonData['projected_cache'] = {}
            return commonData
        
        
        weaponRangeInfos = {}  # {mod.item.ID: rangeInfo}
        maxEffectiveRange = 0
        
        if weaponType == 'turret':
            hardpointType = FittingHardpoint.TURRET
        else:
            hardpointType = FittingHardpoint.MISSILE
        
        for mod in src.item.activeModulesIter():
            # pyfalog.debug(f"DEBUG: Processing module {mod.item.name}, hardpoint={mod.hardpoint}, charge={mod.charge}")
            if mod.hardpoint != hardpointType:
                continue
            if mod.getModifiedItemAttr('miningAmount'):
                continue
            
            key = mod.item.ID
            if key not in weaponRangeInfos:
                if weaponType == 'turret':
                    rangeInfo = getTurretRangeInfo(mod, qualityTier, self.graph._ammo_charge_cache)
                else:
                    if mod.charge is None:
                        chargeCacheKey = (mod.item.ID, qualityTier)
                        validCharges = None
                        if self.graph._ammo_charge_cache is not None and chargeCacheKey in self.graph._ammo_charge_cache:
                             validCharges = self.graph._ammo_charge_cache[chargeCacheKey]
                        
                        if validCharges is None:
                            allCharges = list(getValidChargesForModule(mod))
                            validCharges = filterChargesByQuality(allCharges, qualityTier)
                            if self.graph._ammo_charge_cache is not None:
                                self.graph._ammo_charge_cache[chargeCacheKey] = validCharges
                        
                        if validCharges:
                            tempCharge = validCharges[0]
                            try:
                                mod.charge = tempCharge
                                if mod.owner:
                                    mod.owner.calculated = False
                                    mod.owner.calculateModifiedAttributes()
                                
                                rangeInfo = ranges
                                
                                mod.charge = None
                                if mod.owner:
                                    mod.owner.calculated = False
                                    mod.owner.calculateModifiedAttributes()
                                
                            except Exception as e:
                                pyfalog.error(f"Error simulating charge for {mod.item.name}: {e}")
                                mod.charge = None 
                                if mod.owner:
                                    mod.owner.calculated = False
                                    try:
                                        mod.owner.calculateModifiedAttributes()
                                    except:
                                        pass
                                rangeInfo = None
                        else:
                            rangeInfo = None
                    else:
                        rangeInfo = getLauncherRangeInfo(mod, qualityTier, shipRadius, self.graph._ammo_charge_cache)
                
                if rangeInfo:
                    weaponRangeInfos[key] = rangeInfo
                    if rangeInfo['max_effective_range'] > maxEffectiveRange:
                        maxEffectiveRange = rangeInfo['max_effective_range']
        
        if not weaponRangeInfos:
            commonData['weapon_cache'] = {}
            commonData['projected_cache'] = {}
            return commonData
        
        existingCache = self.graph._ammo_projected_cache.get(projectedCacheKey)
        
        baseTrackingParams = {
            'atkSpeed': atkSpeed,
            'atkAngle': atkAngle,
            'atkRadius': shipRadius,
            'tgtSpeed': tgtSpeed,
            'tgtAngle': tgtAngle,
            'tgtRadius': tgt.getRadius() if tgt else 0,
            'tgtSigRadius': tgtSigRadius
        }
        
        projectedCache = buildProjectedCache(
            src=src,
            tgt=tgt,
            commonData=commonData,
            baseTgtSpeed=tgtSpeed,
            baseTgtSigRadius=tgtSigRadius,
            maxDistance=maxEffectiveRange,
            resolution=1000,  # 1km intervals
            existingCache=existingCache
        )
        
        self.graph._ammo_projected_cache[projectedCacheKey] = projectedCache
        commonData['projected_cache'] = projectedCache
        
        weaponCache = {}
        for mod in src.item.activeModulesIter():
            if mod.hardpoint != hardpointType:
                continue
            if mod.getModifiedItemAttr('miningAmount'):
                continue
            
            key = mod.item.ID
            if key not in weaponCache:
                rangeInfo = weaponRangeInfos.get(key)
                if rangeInfo:
                    if weaponType == 'turret':
                        entry = buildTurretCacheEntry(
                            mod, qualityTier, tgtResists, baseTrackingParams,
                            projectedCache, self.graph._ammo_charge_cache,
                            rangeInfo=rangeInfo
                        )
                    else:
                        entry = buildLauncherCacheEntry(
                            mod, qualityTier, tgtResists, shipRadius,
                            tgtSpeed, tgtSigRadius,
                            projectedCache, self.graph._ammo_charge_cache,
                            rangeInfo=rangeInfo
                        )
                    if entry:
                        weaponCache[key] = entry
            else:
                weaponCache[key]['count'] += 1
        
        # Cache and return
        self.graph._ammo_weapon_cache[weaponCacheKey] = weaponCache
        commonData['weapon_cache'] = weaponCache
        
        return commonData

    def _buildTrackingParams(self, distance, miscParams, src, tgt, commonData):
        tgtSpeed = miscParams.get('tgtSpeed', 0) or 0
        tgtSigRadius = tgt.getSigRadius() if tgt else 0

        if distance == 0:  # Debug logging at distance 0
            sigStr = 'inf' if tgtSigRadius == float('inf') else f"{tgtSigRadius:.1f}"
            pyfalog.debug(f"[TRACKING] Building tracking params: tgtSpeed={tgtSpeed:.1f}, tgtSigRadius={sigStr}")
            pyfalog.debug(f"[TRACKING] tgt={tgt.name if tgt else None}")

        # Only return None if sig radius is exactly 0 (not infinity - that's valid for Ideal Target)
        if tgtSigRadius == 0:
            pyfalog.debug(f"[TRACKING] tgtSigRadius is 0, returning None!")
            return None

        params = {
            'atkSpeed': miscParams.get('atkSpeed', 0) or 0,
            'atkAngle': miscParams.get('atkAngle', 0) or 0,
            'atkRadius': commonData.get('src_radius', 0),
            'tgtSpeed': tgtSpeed,
            'tgtAngle': miscParams.get('tgtAngle', 0) or 0,
            'tgtRadius': tgt.getRadius() if tgt else 0,
            'tgtSigRadius': tgtSigRadius
        }

        if distance == 0:
            pyfalog.debug(f"[TRACKING] Returning params: {params}")

        return params

    def _calculatePoint(self, x, miscParams, src, tgt, commonData):
        weaponCache = commonData.get('weapon_cache', {})
        weaponType = commonData.get('weapon_type')
        if not weaponCache:
            pyfalog.debug(f"[CALC-POINT] No weaponCache for {src.item.name} at distance {x/1000:.1f}km, returning 0")
            return 0

        trackingParams = self._buildTrackingParams(x, miscParams, src, tgt, commonData)
        projectedCache = commonData.get('projected_cache', {})

        if hasattr(self, '_getOptimalDpsAtDistance'):
            result = self._getOptimalDpsAtDistance(x, weaponCache, trackingParams, projectedCache, weaponType)
            if x % 10000 == 0:  # Log every 10km for sampling
                pyfalog.debug(f"[CALC-POINT] {src.item.name} at {x/1000:.1f}km: DPS={result:.1f}")
            return result
        elif hasattr(self, '_getOptimalVolleyAtDistance'):
            result = self._getOptimalVolleyAtDistance(x, weaponCache, trackingParams, projectedCache, weaponType)
            if x % 10000 == 0:  # Log every 10km for sampling
                pyfalog.debug(f"[CALC-POINT] {src.item.name} at {x/1000:.1f}km: Volley={result:.1f}")
            return result
        return 0

    def _calculatePointExtended(self, x, miscParams, src, tgt, commonData):
        weaponCache = commonData.get('weapon_cache', {})
        weaponType = commonData.get('weapon_type')
        if not weaponCache:
            return 0, None
        
        trackingParams = self._buildTrackingParams(x, miscParams, src, tgt, commonData)
        projectedCache = commonData.get('projected_cache', {})
        
        if hasattr(self, '_getOptimalDpsWithAmmoAtDistance'):
            return self._getOptimalDpsWithAmmoAtDistance(x, weaponCache, trackingParams, projectedCache, weaponType)
        elif hasattr(self, '_getOptimalVolleyWithAmmoAtDistance'):
            return self._getOptimalVolleyWithAmmoAtDistance(x, weaponCache, trackingParams, projectedCache, weaponType)
        return 0, None

    def getSegments(self, xRange, miscParams, src, tgt):
        pyfalog.debug(f"[SEGMENTS] ========== getSegments START for src={src.item.name}, tgt={tgt.name if tgt else None} ==========")
        pyfalog.debug(f"[SEGMENTS] xRange={xRange}")
        # Validate xRange - can contain None from range limiters
        minX, maxX = xRange
        if minX is None or maxX is None:
            pyfalog.debug(f"[SEGMENTS] Returning empty - xRange contains None: minX={minX}, maxX={maxX}")
            return []

        pyfalog.debug(f"[SEGMENTS] Calling _getCommonData for {src.item.name}...")
        commonData = self._getCommonData(miscParams=miscParams, src=src, tgt=tgt)
        weaponCache = commonData.get('weapon_cache', {})
        weaponType = commonData.get('weapon_type')
        pyfalog.debug(f"[SEGMENTS] After _getCommonData: weaponType={weaponType}, weaponCache has {len(weaponCache)} groups")
        pyfalog.debug(f"[SEGMENTS] weaponCache id: {id(weaponCache)}")
        
        if not weaponCache:
            pyfalog.debug(f"[SEGMENTS] Returning empty - no weaponCache")
            return []
        
        # Get transitions from first weapon group
        transitions = None
        for groupInfo in weaponCache.values():
            transitions = groupInfo['transitions']
            pyfalog.debug(f"[SEGMENTS] Got {len(transitions) if transitions else 0} transitions from first weapon group")
            break
        
        if not transitions:
            pyfalog.debug(f"[SEGMENTS] Returning empty - no transitions")
            return []
        
        # Filter valid transitions (with ammo name)
        validTransitions = [t for t in transitions if t[2] is not None]
        pyfalog.debug(f"[SEGMENTS] {len(validTransitions)} valid transitions (with ammo name)")
        if not validTransitions:
            pyfalog.debug(f"[SEGMENTS] Returning empty - no valid transitions")
            return []
        
        # Build ammo index mapping
        ammoToIndex = {}
        for t in validTransitions:
            if t[2] not in ammoToIndex:
                ammoToIndex[t[2]] = len(ammoToIndex)
        
        # Generate segments
        segments = []
        
        for i, transition in enumerate(validTransitions):
            transDist, _, ammoName, _ = transition
            segStart = max(transDist, minX)
            
            # Find segment end
            if i + 1 < len(validTransitions):
                segEnd = min(validTransitions[i + 1][0], maxX)
            else:
                segEnd = maxX
            
            if segStart >= segEnd:
                continue
            
            # Generate points at fixed 500m resolution for performance
            step = 500
            xs, ys = [], []
            x = segStart
            while x <= segEnd:
                y = self._calculatePoint(x, miscParams, src, tgt, commonData)
                xs.append(x)
                ys.append(y)
                x += step

            # Always include the segment end point for smooth transitions
            if xs[-1] < segEnd:
                y = self._calculatePoint(segEnd, miscParams, src, tgt, commonData)
                xs.append(segEnd)
                ys.append(y)

            pyfalog.debug(f"[SEGMENTS] Segment {i} ({ammoName}): {len(xs)} points, y_range=[{min(ys) if ys else 'empty'}, {max(ys) if ys else 'empty'}]")

            segments.append({
                'xs': xs,
                'ys': ys,
                'ammo': ammoName,
                'ammoIndex': ammoToIndex[ammoName]
            })

        pyfalog.debug(f"[SEGMENTS] ========== Returning {len(segments)} segments for {src.item.name} ==========")
        return segments

class Distance2OptimalAmmoDpsGetter(XDistanceMixin, YOptimalAmmoDpsMixin):
    def getPointExtended(self, x, miscParams, src, tgt):
        commonData = self._getCommonData(miscParams=miscParams, src=src, tgt=tgt)
        value, ammo = self._calculatePointExtended(x, miscParams, src, tgt, commonData)
        return value, {'ammo': ammo}


class Distance2OptimalAmmoVolleyGetter(XDistanceMixin, YOptimalAmmoVolleyMixin):
    def getPointExtended(self, x, miscParams, src, tgt):
        commonData = self._getCommonData(miscParams=miscParams, src=src, tgt=tgt)
        value, ammo = self._calculatePointExtended(x, miscParams, src, tgt, commonData)
        return value, {'ammo': ammo}

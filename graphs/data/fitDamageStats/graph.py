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


import wx

from eos.const import FittingHardpoint
from graphs.data.base import FitGraph, Input, VectorDef, XDef, YDef
from service.const import GraphCacheCleanupReason
from service.settings import GraphSettings
from .cache import ProjectedDataCache, TimeCache
from .getter import (Distance2DpsGetter, Distance2InflictedDamageGetter, Distance2VolleyGetter, TgtSigRadius2DpsGetter, TgtSigRadius2InflictedDamageGetter,
                     TgtSigRadius2VolleyGetter, TgtSpeed2DpsGetter, TgtSpeed2InflictedDamageGetter, TgtSpeed2VolleyGetter, Time2DpsGetter,
                     Time2InflictedDamageGetter, Time2VolleyGetter)

_t = wx.GetTranslation


class FitDamageStatsGraph(FitGraph):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._timeCache = TimeCache()
        self._projectedCache = ProjectedDataCache()

    def _clearInternalCache(self, reason, extraData):
        # Here, we care only about fit changes and graph changes.
        # - Input changes are irrelevant as time cache cares only about
        # time input, and it regenerates once time goes beyond cached value
        # - Option changes are irrelevant as cache contains "raw" damage
        # values which do not rely on any graph options
        if reason in (GraphCacheCleanupReason.fitChanged, GraphCacheCleanupReason.fitRemoved):
            self._timeCache.clearForFit(extraData)
            self._projectedCache.clearForFit(extraData)
        elif reason == GraphCacheCleanupReason.graphSwitched:
            self._timeCache.clearAll()
            self._projectedCache.clearAll()

    # UI stuff
    internalName = 'dmgStatsGraph'
    name = _t('Damage Stats')
    xDefs = [
        XDef(handle='distance', unit='km', label=_t('Distance'), mainInput=('distance', 'km')),
        XDef(handle='time', unit='s', label=_t('Time'), mainInput=('time', 's')),
        XDef(handle='tgtSpeed', unit='m/s', label=_t('Target speed'), mainInput=('tgtSpeed', '%')),
        XDef(handle='tgtSpeed', unit='%', label=_t('Target speed'), mainInput=('tgtSpeed', '%')),
        XDef(handle='tgtSigRad', unit='m', label=_t('Target signature radius'), mainInput=('tgtSigRad', '%')),
        XDef(handle='tgtSigRad', unit='%', label=_t('Target signature radius'), mainInput=('tgtSigRad', '%'))]
    inputs = [
        Input(handle='distance', unit='km', label=_t('Distance'), iconID=1391, defaultValue=None, defaultRange=(0, 100),
              mainTooltip=_t('Distance between the attacker and the target, as seen in overview (surface-to-surface)'),
              secondaryTooltip=_t('Distance between the attacker and the target, as seen in overview (surface-to-surface)\nWhen set, places the target that far away from the attacker\nWhen not set, attacker\'s weapons always hit the target')),
        Input(handle='time', unit='s', label=_t('Time'), iconID=1392, defaultValue=None, defaultRange=(0, 80),
              secondaryTooltip=_t('When set, uses attacker\'s exact damage stats at a given time\nWhen not set, uses attacker\'s damage stats as shown in stats panel of main window')),
        Input(handle='tgtSpeed', unit='%', label=_t('Target speed'), iconID=1389, defaultValue=100, defaultRange=(0, 100)),
        Input(handle='tgtSigRad', unit='%', label=_t('Target signature'), iconID=1390, defaultValue=100, defaultRange=(100, 200), conditions=[
            (('tgtSigRad', 'm'), None),
            (('tgtSigRad', '%'), None)])]
    srcVectorDef = VectorDef(lengthHandle='atkSpeed', lengthUnit='%', angleHandle='atkAngle', angleUnit='degrees', label=_t('Attacker'))
    tgtVectorDef = VectorDef(lengthHandle='tgtSpeed', lengthUnit='%', angleHandle='tgtAngle', angleUnit='degrees', label=_t('Target'))
    hasTargets = True
    srcExtraCols = ('Dps', 'Volley', 'Speed', 'Radius')

    @property
    def yDefs(self):
        ignoreResists = GraphSettings.getInstance().get('ignoreResists')
        return [
            YDef(handle='dps', unit=None, label=_t('DPS') if ignoreResists else _t('Effective DPS')),
            YDef(handle='volley', unit=None, label=_t('Volley') if ignoreResists else _t('Effective volley')),
            YDef(handle='damage', unit=None, label=_t('Damage inflicted') if ignoreResists else _t('Effective damage inflicted'))]

    @property
    def tgtExtraCols(self):
        cols = []
        if not GraphSettings.getInstance().get('ignoreResists'):
            cols.append('Target Resists')
        cols.extend(('Speed', 'SigRadius', 'Radius', 'FullHP'))
        return cols

    # Calculation stuff
    _normalizers = {
        ('distance', 'km'): lambda v, src, tgt: None if v is None else v * 1000,
        ('atkSpeed', '%'): lambda v, src, tgt: v / 100 * src.getMaxVelocity(),
        ('tgtSpeed', '%'): lambda v, src, tgt: v / 100 * tgt.getMaxVelocity(),
        ('tgtSigRad', '%'): lambda v, src, tgt: v / 100 * tgt.getSigRadius()}
    _limiters = {'time': lambda src, tgt: (0, 2500)}
    _getters = {
        ('distance', 'dps'): Distance2DpsGetter,
        ('distance', 'volley'): Distance2VolleyGetter,
        ('distance', 'damage'): Distance2InflictedDamageGetter,
        ('time', 'dps'): Time2DpsGetter,
        ('time', 'volley'): Time2VolleyGetter,
        ('time', 'damage'): Time2InflictedDamageGetter,
        ('tgtSpeed', 'dps'): TgtSpeed2DpsGetter,
        ('tgtSpeed', 'volley'): TgtSpeed2VolleyGetter,
        ('tgtSpeed', 'damage'): TgtSpeed2InflictedDamageGetter,
        ('tgtSigRad', 'dps'): TgtSigRadius2DpsGetter,
        ('tgtSigRad', 'volley'): TgtSigRadius2VolleyGetter,
        ('tgtSigRad', 'damage'): TgtSigRadius2InflictedDamageGetter}
    _denormalizers = {
        ('distance', 'km'): lambda v, src, tgt: None if v is None else v / 1000,
        ('tgtSpeed', '%'): lambda v, src, tgt: v * 100 / tgt.getMaxVelocity(),
        ('tgtSigRad', '%'): lambda v, src, tgt: v * 100 / tgt.getSigRadius()}

    def getDefaultInputRange(self, inputDef, sources):
        """
        Calculate dynamic default range based on the turrets/missiles max effective range.
        
        Returns (min, max) tuple in the input's units (km for distance).
        For turrets: the longest range weapon's optimal+falloff*2 + 10%, capped at 300km.
        For missiles: the longest range missile's max range + 10%, capped at 300km.
        """
        if inputDef.handle != 'distance' or not sources:
            return inputDef.defaultRange
        
        max_range_m = 0
        
        for src in sources:
            fit = src.item
            if fit is None:
                continue
            
            # Check all turrets and missiles
            for mod in fit.activeModulesIter():
                if mod.hardpoint == FittingHardpoint.TURRET:
                    if mod.getModifiedItemAttr('miningAmount'):
                        continue
                    
                    # Get turret optimal and falloff
                    optimal = mod.getModifiedItemAttr('maxRange') or 0
                    falloff = mod.getModifiedItemAttr('falloff') or 0
                    
                    # Check all compatible charges for range modifiers
                    for charge in mod.getValidCharges():
                        rangeMultiplier = charge.getAttribute('weaponRangeMultiplier') or 1
                        falloffMultiplier = charge.getAttribute('fallofMultiplier') or 1
                        
                        # Calculate effective optimal + 2*falloff (where DPS drops to ~6%)
                        effective_optimal = optimal * rangeMultiplier
                        effective_falloff = falloff * falloffMultiplier
                        effective_max = effective_optimal + effective_falloff * 2
                        
                        if effective_max > max_range_m:
                            max_range_m = effective_max
                
                elif mod.hardpoint == FittingHardpoint.MISSILE:
                    # For missiles, use missileMaxRangeData if charge loaded
                    if mod.charge is not None:
                        missileRangeData = mod.missileMaxRangeData
                        if missileRangeData:
                            _, higherRange, _ = missileRangeData
                            if higherRange > max_range_m:
                                max_range_m = higherRange
                    else:
                        # Estimate from compatible charges using base attributes
                        for charge in mod.getValidCharges():
                            maxVelocity = charge.getAttribute('maxVelocity') or 0
                            explosionDelay = charge.getAttribute('explosionDelay') or 0
                            if maxVelocity > 0 and explosionDelay > 0:
                                # Simple estimate: velocity * flight_time
                                flightTime = explosionDelay / 1000
                                estimated_range = maxVelocity * flightTime
                                if estimated_range > max_range_m:
                                    max_range_m = estimated_range
        
        if max_range_m <= 0:
            return inputDef.defaultRange
        
        # Add 10% buffer and convert to km
        max_range_km = (max_range_m * 1.10) / 1000
        
        # Cap at 300km (EVE's max lock range)
        max_range_km = min(max_range_km, 300)
        
        # Round to nice number
        max_range_km = int(max_range_km + 0.5)
        
        return (0, max_range_km)

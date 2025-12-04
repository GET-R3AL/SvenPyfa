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

import re
import math
from logbook import Logger

from eos.const import FittingHardpoint
from eos.saveddata.fit import Fit
from graphs.data.base import FitGraph, XDef, YDef, Input
from graphs.data.fitAmmoOptimalDps.getter import (
    Distance2OptimalAmmoDpsGetter,
    Distance2OptimalAmmoVolleyGetter,
    get_ammo_name_at_distance_fast,
    get_turret_base_stats,
    get_charge_stats)
from graphs.data.fitDamageStats.cache import ProjectedDataCache
from service.const import GraphCacheCleanupReason
from service.settings import GraphSettings

pyfalog = Logger(__name__)


# Ammo color definitions (RGB tuples, 0-1 range)
# Colors are assigned based on ammo base name (without size suffix)
AMMO_COLORS = {
    # Hybrid - Short Range
    "Null": (0.7, 0.7, 0.65),
    "Void": (0.5, 0.1, 0.2),
    # Hybrid - Long Range
    "Spike": (0.761, 1, 0.169),
    "Javelin": (0.439, 0.984, 0),
    # Hybrid - Standard
    "Antimatter": (0.7, 0.1, 0.7),
    "Iridium": (0.1, 0.7, 0.7),
    "Lead": (0.7, 0.7, 0.1),
    "Plutonium": (0.431, 0.871, 1),
    "Thorium": (0.7, 0.1, 0.1),
    "Uranium": (0.102, 0.839, 0.631),
    "Tungsten": (0.3, 0.3, 0.6),
    "Iron": (0.6, 0.3, 0.3),
    
    # Laser - Short Range
    "Scorch": (0.922, 0.31, 1),
    "Conflagration": (0, 0.722, 0.251),
    # Laser - Long Range
    "Gleam": (0.71, 0.569, 0.369),
    "Aurora": (0.651, 0.071, 0.216),
    # Laser - Standard
    "Multifrequency": (0.8, 0.8, 0.8),
    "Gamma": (0.02, 0.4, 0.949),
    "Xray": (0, 0.741, 0.525),
    "Ultraviolet": (0.42, 0, 0.741),
    "Standard": (0.9, 0.7, 0.0),
    "Infrared": (0.949, 0.251, 0.02),
    "Microwave": (0.949, 0.557, 0.02),
    "Radio": (0.89, 0.039, 0.039),
    
    # Projectile - Short Range
    "Quake": (0.78, 0.604, 0.322),
    "Hail": (1.0, 0.6, 0),
    # Projectile - Long Range
    "Tremor": (0.29, 0.251, 0.184),
    "Barrage": (0.769, 0.325, 0.008),
    # Projectile - Standard
    "Carbonized Lead": (0.753, 0.318, 0.839),
    "Depleted Uranium": (0.404, 0, 0.812),
    "EMP": (0.098, 0.761, 0.761),
    "Fusion": (0.871, 0.549, 0.129),
    "Nuclear": (0.478, 0.722, 0.059),
    "Phased Plasma": (0.722, 0.059, 0.212),
    "Proton": (0.216, 0.455, 0.459),
    "Titanium Sabot": (0.212, 0.294, 0.369),
}


def get_ammo_base_name(ammo_name):
    """
    Extract base ammo name by removing size suffix (S/M/L/XL) and other common suffixes.
    
    Examples:
        "Conflagration L" -> "Conflagration"
        "Void S" -> "Void"
        "Antimatter Charge XL" -> "Antimatter"
        "Republic Fleet EMP M" -> "EMP"
    """
    if not ammo_name:
        return None
    
    # Remove common suffixes: size letters, "Charge", faction prefixes
    # Pattern: remove trailing " S", " M", " L", " XL" and " Charge"
    cleaned = re.sub(r'\s+(S|M|L|XL)$', '', ammo_name, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s+Charge$', '', cleaned, flags=re.IGNORECASE)
    
    # Remove faction prefixes (e.g., "Republic Fleet ", "Imperial Navy ", "Caldari Navy ")
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


def get_ammo_color(ammo_name):
    """
    Get RGB color tuple for an ammo type.
    Returns None if no color defined for this ammo.
    """
    base_name = get_ammo_base_name(ammo_name)
    if base_name in AMMO_COLORS:
        return AMMO_COLORS[base_name]
    
    # Try partial match for ammo names that might have extra words
    for key in AMMO_COLORS:
        if key in base_name or base_name in key:
            return AMMO_COLORS[key]
    
    return None


class FitAmmoOptimalDpsGraph(FitGraph):

    # Graph definition
    internalName = 'ammoOptimalDpsGraph'
    name = 'Application Profile'
    xDefs = [
        XDef(handle='distance', unit='km', label='Distance', mainInput=('distance', 'km'))]
    yDefs = [
        YDef(handle='dps', unit=None, label='DPS'),
        YDef(handle='volley', unit=None, label='Volley')]
    inputs = [
        Input(handle='distance', unit='km', label='Distance', iconID=None, defaultValue=None, defaultRange=(0, 100), mainTooltip='Distance to target')]
    sources = {Fit}
    _limitToOutgoingProjected = True
    hasTargets = True

    # Normalizers convert input values to internal units (km to meters)
    _normalizers = {
        ('distance', 'km'): lambda v, src, tgt: None if v is None else v * 1000}
    
    # Denormalizers convert internal units back to display units (meters to km)
    _denormalizers = {
        ('distance', 'km'): lambda v, src, tgt: None if v is None else v / 1000}
    
    # No limiters - allow user to specify any range they want
    _limiters = {}

    # Getter mapping
    _getters = {
        ('distance', 'dps'): Distance2OptimalAmmoDpsGetter,
        ('distance', 'volley'): Distance2OptimalAmmoVolleyGetter}
    
    # Enable segmented plotting for this graph
    hasSegments = True
    
    # Ammo color mode: True = use ammo-specific colors, False = use line patterns
    useAmmoColors = True

    def __init__(self):
        super().__init__()
        self._projectedCache = ProjectedDataCache()
    
    def getAmmoColor(self, ammoName):
        """Get RGB color tuple for an ammo type."""
        return get_ammo_color(ammoName)

    def getDefaultInputRange(self, inputDef, sources):
        """
        Calculate dynamic default range based on the turrets' max effective range.
        
        Returns (min, max) tuple in the input's units (km for distance).
        The max is the longest range ammo's optimal+falloff*2 + 5%, capped at 300km.
        """
        if inputDef.handle != 'distance' or not sources:
            return inputDef.defaultRange
        
        max_range_m = 0
        
        for src in sources:
            fit = src.item
            if fit is None:
                continue
            
            # Check all turrets and their compatible charges
            for mod in fit.activeModulesIter():
                if mod.hardpoint != FittingHardpoint.TURRET:
                    continue
                if mod.getModifiedItemAttr('miningAmount'):
                    continue
                
                # Get turret base stats
                turret_base = get_turret_base_stats(mod)
                
                # Check all compatible charges for this turret
                for charge in mod.getValidCharges():
                    charge_stats = get_charge_stats(charge)
                    
                    # Calculate effective optimal + 2*falloff (where DPS drops to ~6%)
                    effective_optimal = turret_base['optimal'] * charge_stats['rangeMultiplier']
                    effective_falloff = turret_base['falloff'] * charge_stats['falloffMultiplier']
                    effective_max = effective_optimal + effective_falloff * 2
                    
                    if effective_max > max_range_m:
                        max_range_m = effective_max
        
        if max_range_m <= 0:
            return inputDef.defaultRange
        
        # Add 5% buffer and convert to km
        max_range_km = (max_range_m * 1.05) / 1000
        
        # Cap at 300km (EVE's max lock range)
        max_range_km = min(max_range_km, 300)
        
        # Round to nice number
        max_range_km = int(max_range_km + 0.5)
        
        return (0, max_range_km)

    def _clearInternalCache(self, reason, extraData):
        if reason in (GraphCacheCleanupReason.fitChanged, GraphCacheCleanupReason.fitRemoved):
            self._projectedCache.clearForFit(extraData)
            # Clear ammo cache
            if hasattr(self, '_ammo_turret_cache'):
                self._ammo_turret_cache = {}
            if hasattr(self, '_ammo_analysis_done'):
                self._ammo_analysis_done = set()
        elif reason == GraphCacheCleanupReason.graphSwitched:
            self._projectedCache.clearAll()
            # Clear ammo cache
            if hasattr(self, '_ammo_turret_cache'):
                self._ammo_turret_cache = {}
            if hasattr(self, '_ammo_analysis_done'):
                self._ammo_analysis_done = set()

    def getPlotSegments(self, mainInput, miscInputs, xSpec, ySpec, src, tgt=None):
        """
        Get segmented plot data with ammo information for color coding.
        
        Returns list of segments, each with xs, ys, ammo name, and ammo index.
        Returns None if this graph doesn't support segments or getter doesn't have getSegments.
        """
        try:
            getterClass = self._getters[(xSpec.handle, ySpec.handle)]
        except KeyError:
            return None
        
        # Normalize the input range
        mainParamRange = self._normalizeMain(mainInput=mainInput, src=src, tgt=tgt)
        miscParams = self._normalizeMisc(miscInputs=miscInputs, src=src, tgt=tgt)
        mainParamRange = self._limitMain(mainParamRange=mainParamRange, src=src, tgt=tgt)
        miscParams = self._limitMisc(miscParams=miscParams, src=src, tgt=tgt)
        
        getter = getterClass(graph=self)
        
        # Check if getter has getSegments method
        if not hasattr(getter, 'getSegments'):
            return None
        
        segments = getter.getSegments(
            xRange=mainParamRange[1], 
            miscParams=miscParams, 
            src=src, 
            tgt=tgt)
        
        if not segments:
            return None
        
        # Denormalize the values back to display units
        for segment in segments:
            segment['xs'] = self._denormalizeValues(values=segment['xs'], axisSpec=xSpec, src=src, tgt=tgt)
            segment['ys'] = self._denormalizeValues(values=segment['ys'], axisSpec=ySpec, src=src, tgt=tgt)
        
        return segments

    def getPointExtended(self, x, miscInputs, xSpec, ySpec, src, tgt=None):
        """
        Get point value with extended info (like ammo name) at x.
        
        Returns (y_value, extra_info_dict) tuple.
        extra_info_dict may contain 'ammo' key with the ammo name.
        """
        try:
            getterClass = self._getters[(xSpec.handle, ySpec.handle)]
        except KeyError:
            return None, {}
        
        x = self._normalizeValue(value=x, axisSpec=xSpec, src=src, tgt=tgt)
        miscParams = self._normalizeMisc(miscInputs=miscInputs, src=src, tgt=tgt)
        miscParams = self._limitMisc(miscParams=miscParams, src=src, tgt=tgt)
        
        getter = getterClass(graph=self)
        
        # Check if getter has getPointExtended method
        if hasattr(getter, 'getPointExtended'):
            y, extraInfo = getter.getPointExtended(x=x, miscParams=miscParams, src=src, tgt=tgt)
            y = self._denormalizeValue(value=y, axisSpec=ySpec, src=src, tgt=tgt)
            return y, extraInfo
        else:
            # Fall back to regular getPoint
            y = self._getPoint(x=x, miscParams=miscParams, xSpec=xSpec, ySpec=ySpec, src=src, tgt=tgt)
            y = self._denormalizeValue(value=y, axisSpec=ySpec, src=src, tgt=tgt)
            return y, {}

    def getAmmoNameFast(self, x, xSpec, src):
        """
        Ultra-fast ammo name lookup using cached transition data.
        
        Used during drag operations for real-time ammo display without
        recalculating DPS. O(log n) using binary search on transitions.
        
        Returns ammo name (str) or None if no cache available.
        """
        # Normalize distance (km to meters)
        distance = self._normalizeValue(value=x, axisSpec=xSpec, src=src, tgt=None)
        if distance is None:
            return None
        
        # Look up in cache
        cache_key = id(src.item)
        if not hasattr(self, '_ammo_turret_cache') or cache_key not in self._ammo_turret_cache:
            return None
        
        turret_cache = self._ammo_turret_cache[cache_key]
        if not turret_cache:
            return None
        
        # Get ammo name from first turret group (they should all use same ammo)
        for group_info in turret_cache.values():
            transitions = group_info.get('transitions')
            if transitions:
                return get_ammo_name_at_distance_fast(transitions, distance)
        
        return None

    def _updateMiscParams(self, **kwargs):
        miscParams = super()._updateMiscParams(**kwargs)
        # Set defaults from target profile
        miscParams['tgtSigRadius'] = miscParams['tgt'].getSigRadius()
        miscParams['tgtSpeed'] = miscParams['tgt'].getMaxVelocity()
        miscParams.setdefault('atkSpeed', 0)
        miscParams.setdefault('atkAngle', 0)
        miscParams.setdefault('tgtAngle', 0)
        return miscParams

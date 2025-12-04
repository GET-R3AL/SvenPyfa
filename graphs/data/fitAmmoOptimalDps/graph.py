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
from graphs.data.base import FitGraph, XDef, YDef, Input, VectorDef
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


# Ammo color definitions (RGB tuples, 0-255 range)
# Colors are assigned based on ammo base name (without size suffix)
AMMO_COLORS = {
    # Hybrid - Short Range
    "Null": (179, 179, 166),
    "Void": (128, 26, 51),
    # Hybrid - Long Range
    "Spike": (194, 255, 43),
    "Javelin": (112, 251, 0),
    # Hybrid - Standard
    "Antimatter": (179, 26, 179),
    "Iridium": (26, 179, 179),
    "Lead": (179, 179, 26),
    "Plutonium": (110, 222, 255),
    "Thorium": (179, 26, 26),
    "Uranium": (26, 214, 161),
    "Tungsten": (77, 77, 153),
    "Iron": (153, 77, 77),
    
    # Laser - Short Range
    "Scorch": (235, 79, 255),
    "Conflagration": (0, 184, 64),
    # Laser - Long Range
    "Gleam": (181, 145, 94),
    "Aurora": (166, 18, 55),
    # Laser - Standard
    "Multifrequency": (204, 204, 204),
    "Gamma": (5, 102, 242),
    "Xray": (0, 189, 134),
    "Ultraviolet": (107, 0, 189),
    "Standard": (230, 179, 0),
    "Infrared": (242, 64, 5),
    "Microwave": (242, 142, 5),
    "Radio": (227, 10, 10),
    
    # Projectile - Short Range
    "Quake": (199, 154, 82),
    "Hail": (255, 153, 0),
    # Projectile - Long Range
    "Tremor": (74, 64, 47),
    "Barrage": (196, 83, 2),
    # Projectile - Standard
    "Carbonized Lead": (192, 81, 214),
    "Depleted Uranium": (103, 0, 207),
    "EMP": (25, 194, 194),
    "Fusion": (222, 140, 33),
    "Nuclear": (122, 184, 15),
    "Phased Plasma": (184, 15, 54),
    "Proton": (55, 116, 117),
    "Titanium Sabot": (54, 75, 94),

    # Exotic Plasma - Advanced
    "Occult": (189,0,38),
    "Mystic": (252,174,145),

    # Exotic Plasma - Standard
    "Tetryon": (240,59,32),
    "Baryon": (253,141,60),
    "Meson": (254,204,92),

    # Vorton Charges - Advanced
    "ElectroPunch Ultra": (37,52,148),
    "StrikeSnipe Ultra": (103,169,207),

    # Vorton Charges - Standard
    "BlastShot Condenser Pack": (49,163,84),
    "GalvaSurge Condenser Pack": (44,127,184),
    "MesmerFlux Condenser Pack": (65,182,196),
    "SlamBolt Condenser Pack": (194,230,153),

    # =========================================================================
    # MISSILE AMMO COLORS
    # Based on damage type with saturation/brightness variants:
    # - Rage/Fury: Dark, highly saturated (highest damage)
    # - Faction (Caldari Navy, Dread Guristas, etc.): Medium-dark, saturated
    # - Precision/Javelin: Medium, less saturated (better application)
    # - T1 Standard: Light, least saturated (baseline)
    # =========================================================================
    
    # EM Damage (Mjolnir) - Blue tones
    # Rage/Fury variants (dark, saturated)
    "Mjolnir Rage": (20, 60, 200),
    "Mjolnir Fury": (20, 60, 200),
    # Faction variants (medium-dark, saturated) - covers Caldari Navy, Dread Guristas, etc.
    "Faction Mjolnir": (50, 100, 220),
    # Precision/Javelin variants (medium, less saturated)
    "Mjolnir Precision": (90, 130, 210),
    "Mjolnir Javelin": (90, 130, 210),
    # T1 Standard (light, least saturated)
    "Mjolnir": (140, 165, 220),
    
    # Thermal Damage (Inferno) - Red/Orange tones
    # Rage/Fury variants (dark, saturated)
    "Inferno Rage": (200, 30, 20),
    "Inferno Fury": (200, 30, 20),
    # Faction variants (medium-dark, saturated)
    "Faction Inferno": (220, 70, 50),
    # Precision/Javelin variants (medium, less saturated)
    "Inferno Precision": (210, 110, 90),
    "Inferno Javelin": (210, 110, 90),
    # T1 Standard (light, least saturated)
    "Inferno": (220, 150, 140),
    
    # Kinetic Damage (Scourge) - Gray/White with slight green tint
    # Rage/Fury variants (dark, saturated)
    "Scourge Rage": (50, 80, 60),
    "Scourge Fury": (50, 80, 60),
    # Faction variants (medium-dark, saturated)
    "Faction Scourge": (90, 120, 100),
    # Precision/Javelin variants (medium, less saturated)
    "Scourge Precision": (130, 155, 140),
    "Scourge Javelin": (130, 155, 140),
    # T1 Standard (light, least saturated)
    "Scourge": (175, 195, 185),
    
    # Explosive Damage (Nova) - Yellow/Orange tones
    # Rage/Fury variants (dark, saturated)
    "Nova Rage": (200, 140, 0),
    "Nova Fury": (200, 140, 0),
    # Faction variants (medium-dark, saturated)
    "Faction Nova": (220, 170, 40),
    # Precision/Javelin variants (medium, less saturated)
    "Nova Precision": (220, 190, 90),
    "Nova Javelin": (220, 190, 90),
    # T1 Standard (light, least saturated)
    "Nova": (230, 210, 150),

}


def get_ammo_base_name(ammo_name):
    """
    Extract base ammo name by removing size suffix (S/M/L/XL), missile type suffixes, and other common suffixes.
    
    Examples:
        "Conflagration L" -> "Conflagration"
        "Void S" -> "Void"
        "Antimatter Charge XL" -> "Antimatter"
        "Republic Fleet EMP M" -> "EMP"
        "Mjolnir Rage Light Missile" -> "Mjolnir Rage"
        "Caldari Navy Scourge Heavy Missile" -> "Caldari Navy Scourge"
        "Nova Fury Torpedo" -> "Nova Fury"
    """
    if not ammo_name:
        return None
    
    cleaned = ammo_name
    
    # Remove missile type suffixes (e.g., "Light Missile", "Heavy Assault Missile", "Torpedo", "Cruise Missile")
    missile_suffixes = [
        ' XL Torpedo', ' XL Cruise Missile',  # XL variants first (longest match)
        ' Light Missile', ' Heavy Missile', ' Heavy Assault Missile',
        ' Cruise Missile', ' Torpedo', ' Auto-Targeting Missile',
        ' Defender Missile',
    ]
    is_missile = False
    for suffix in missile_suffixes:
        if cleaned.endswith(suffix):
            cleaned = cleaned[:-len(suffix)]
            is_missile = True
            break
    
    # For turret ammo, remove faction prefixes (e.g., "Republic Fleet ", "Imperial Navy ", "Caldari Navy ")
    # For missiles, keep faction prefix as it indicates ammo quality
    if not is_missile:
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
    
    # Remove common turret ammo suffixes: size letters, "Charge"
    # Pattern: remove trailing " S", " M", " L", " XL" and " Charge"
    cleaned = re.sub(r'\s+(S|M|L|XL)$', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s+Charge$', '', cleaned, flags=re.IGNORECASE)
    
    return cleaned


# Missile damage type base names for faction lookup
MISSILE_DAMAGE_TYPES = {'Mjolnir', 'Inferno', 'Scourge', 'Nova'}

# Faction prefixes to normalize for missile color lookup
FACTION_PREFIXES = [
    'Caldari Navy ', 'Dread Guristas ', 'True Sansha ', 'Shadow Serpentis ', 
    'Domination ', 'Dark Blood ', "Arch Angel ", 'Guristas ', 'Sansha ', 
    'Serpentis ', 'Blood ', 'Angel ', 'Republic Fleet ', 'Imperial Navy ', 
    'Federation Navy '
]


def get_ammo_color(ammo_name):
    """
    Get RGB color tuple for an ammo type.
    Returns color in 0-1 range for matplotlib, or None if no color defined.
    """
    base_name = get_ammo_base_name(ammo_name)
    if not base_name:
        return None
    
    color = None
    
    # Direct lookup first
    if base_name in AMMO_COLORS:
        color = AMMO_COLORS[base_name]
    else:
        # For faction missiles, normalize to "Faction <DamageType>" lookup
        # e.g., "Caldari Navy Mjolnir" -> try "Faction Mjolnir"
        for prefix in FACTION_PREFIXES:
            if base_name.startswith(prefix):
                faction_normalized = 'Faction ' + base_name[len(prefix):]
                if faction_normalized in AMMO_COLORS:
                    color = AMMO_COLORS[faction_normalized]
                    break
        
        # If still not found, try partial match for turret ammo names
        if color is None:
            for key in AMMO_COLORS:
                if key in base_name or base_name in key:
                    color = AMMO_COLORS[key]
                    break
    
    # Convert from 0-255 to 0-1 range for matplotlib
    if color:
        return (color[0] / 255, color[1] / 255, color[2] / 255)
    return None


class FitAmmoOptimalDpsGraph(FitGraph):

    # Graph definition
    internalName = 'ammoOptimalDpsGraph'
    name = 'Application Profile'
    xDefs = [
        XDef(handle='distance', unit='km', label='Distance', mainInput=('distance', 'km'))]
    inputs = [
        Input(handle='distance', unit='km', label='Distance', iconID=None, defaultValue=None, defaultRange=(0, 100), mainTooltip='Distance to target')]
    
    # Vector controls for attacker and target velocity/angle (same as DPS graph)
    srcVectorDef = VectorDef(lengthHandle='atkSpeed', lengthUnit='%', angleHandle='atkAngle', angleUnit='degrees', label='Attacker')
    tgtVectorDef = VectorDef(lengthHandle='tgtSpeed', lengthUnit='%', angleHandle='tgtAngle', angleUnit='degrees', label='Target')
    
    sources = {Fit}
    _limitToOutgoingProjected = True
    hasTargets = True
    srcExtraCols = ('Speed', 'Radius')

    @property
    def yDefs(self):
        ignoreResists = GraphSettings.getInstance().get('ammoOptimalIgnoreResists')
        return [
            YDef(handle='dps', unit=None, label='DPS' if ignoreResists else 'Effective DPS'),
            YDef(handle='volley', unit=None, label='Volley' if ignoreResists else 'Effective Volley')]

    # Normalizers convert input values to internal units
    _normalizers = {
        ('distance', 'km'): lambda v, src, tgt: None if v is None else v * 1000,
        ('atkSpeed', '%'): lambda v, src, tgt: v / 100 * src.getMaxVelocity(),
        ('tgtSpeed', '%'): lambda v, src, tgt: v / 100 * tgt.getMaxVelocity()}
    
    # Denormalizers convert internal units back to display units
    _denormalizers = {
        ('distance', 'km'): lambda v, src, tgt: None if v is None else v / 1000,
        ('tgtSpeed', '%'): lambda v, src, tgt: v * 100 / tgt.getMaxVelocity()}
    
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

    @property
    def tgtExtraCols(self):
        """Show target resists in the target list when not ignoring them."""
        cols = []
        if not GraphSettings.getInstance().get('ammoOptimalIgnoreResists'):
            cols.append('Target Resists')
        return cols

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
        max_range_km = (max_range_m * 1.10) / 1000
        
        # Cap at 300km (EVE's max lock range)
        max_range_km = min(max_range_km, 300)
        
        # Round to nice number
        max_range_km = int(max_range_km + 0.5)
        
        return (0, max_range_km)

    def _clearInternalCache(self, reason, extraData):
        if reason in (GraphCacheCleanupReason.fitChanged, GraphCacheCleanupReason.fitRemoved):
            self._projectedCache.clearForFit(extraData)
            # Clear ammo caches
            if hasattr(self, '_ammo_turret_cache'):
                self._ammo_turret_cache = {}
            if hasattr(self, '_ammo_missile_cache'):
                self._ammo_missile_cache = {}
            if hasattr(self, '_ammo_analysis_done'):
                self._ammo_analysis_done = set()
        elif reason == GraphCacheCleanupReason.graphSwitched:
            self._projectedCache.clearAll()
            # Clear ammo caches
            if hasattr(self, '_ammo_turret_cache'):
                self._ammo_turret_cache = {}
            if hasattr(self, '_ammo_missile_cache'):
                self._ammo_missile_cache = {}
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

    def getAmmoNameFast(self, x, xSpec, src, tgt=None):
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
        
        # Look up in cache - cache key is (fit_id, quality_tier, tgt_resists)
        if not hasattr(self, '_ammo_turret_cache'):
            return None
        
        # Build the same cache key used in the getter
        fit_id = id(src.item)
        quality_tier = getattr(self, '_ammoQuality', 'all')
        
        # Get target resists if not ignoring them
        ignore_resists = GraphSettings.getInstance().get('ammoOptimalIgnoreResists')
        if ignore_resists or tgt is None:
            tgt_resists = None
        else:
            tgt_resists = tgt.getResists()
        
        cache_key = (fit_id, quality_tier, tgt_resists)
        
        if cache_key not in self._ammo_turret_cache:
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

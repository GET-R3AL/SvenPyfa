import colorsys
import math
import re
from logbook import Logger

from eos.const import FittingHardpoint
from eos.saveddata.fit import Fit
from graphs.data.base import FitGraph, XDef, YDef, Input, VectorDef
from graphs.data.fitAmmoOptimalDps.getter import (
    Distance2OptimalAmmoDpsGetter,
    Distance2OptimalAmmoVolleyGetter,
)
from graphs.data.fitAmmoOptimalDps.calc.turret import getTurretBaseStats
from graphs.data.fitAmmoOptimalDps.calc.charges import getChargeStats
from graphs.data.fitAmmoOptimalDps.calc.optimize_ammo import getAmmoNameAtDistance
from graphs.data.fitAmmoOptimalDps.calc.launcher import getFlightMultipliers
from graphs.data.fitDamageStats.cache import ProjectedDataCache
from service.const import GraphCacheCleanupReason
from service.settings import GraphSettings

pyfalog = Logger(__name__)


# Ammo color definitions (RGB tuples, 0-255 range)
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
    
    # Energy - Short Range
    "Scorch": (235, 79, 255),
    "Conflagration": (0, 184, 64),
    # Energy - Long Range
    "Gleam": (181, 145, 94),
    "Aurora": (166, 18, 55),
    # Energy - Standard
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
}

# Missile damage type hues (0-360 degrees)
MISSILE_DAMAGE_HUES = {
    'Mjolnir': 210,   # Blue (EM)
    'Inferno': 0,     # Red (Thermal)
    'Scourge': 180,   # Cyan/Teal (Kinetic)
    'Nova': 30,       # Orange (Explosive)
}

# Charge type saturation and value/brightness (0-100 scale)
MISSILE_CHARGE_SV = {
    'Rage': (90, 55),
    'Fury': (90, 55),
    'Faction': (55, 90),
    'Precision': (50, 85),
    'Javelin': (50, 45),
    'T1': (25, 90),
}


def _hsv_to_rgb_255(h, s, v):
    """Convert HSV (h: 0-360, s: 0-100, v: 0-100) to RGB (0-255)."""
    r, g, b = colorsys.hsv_to_rgb(h / 360, s / 100, v / 100)
    return (int(r * 255), int(g * 255), int(b * 255))


def _generate_missile_colors():
    """Generate missile ammo colors based on damage type hue and charge type sat/brightness."""
    colors = {}
    
    for damage_type, hue in MISSILE_DAMAGE_HUES.items():
        # Rage variant
        s, v = MISSILE_CHARGE_SV['Rage']
        colors[f"{damage_type} Rage"] = _hsv_to_rgb_255(hue, s, v)
        
        # Fury variant
        s, v = MISSILE_CHARGE_SV['Fury']
        colors[f"{damage_type} Fury"] = _hsv_to_rgb_255(hue, s, v)
        
        # Faction variant
        s, v = MISSILE_CHARGE_SV['Faction']
        colors[f"Faction {damage_type}"] = _hsv_to_rgb_255(hue, s, v)
        
        # Precision variant
        s, v = MISSILE_CHARGE_SV['Precision']
        colors[f"{damage_type} Precision"] = _hsv_to_rgb_255(hue, s, v)
        
        # Javelin variant
        s, v = MISSILE_CHARGE_SV['Javelin']
        colors[f"{damage_type} Javelin"] = _hsv_to_rgb_255(hue, s, v)
        
        # T1 Standard (just damage type name)
        s, v = MISSILE_CHARGE_SV['T1']
        colors[damage_type] = _hsv_to_rgb_255(hue, s, v)
    
    return colors

# Add generated missile colors to AMMO_COLORS
AMMO_COLORS.update(_generate_missile_colors())


def get_ammo_base_name(ammo_name):
    """
    Extract base ammo name by removing size suffix (S/M/L/XL), missile type suffixes, and other common suffixes.
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
        Calculate dynamic default range based on the turrets/missiles max effective range.
        
        Returns (min, max) tuple in the input's units (km for distance).
        For turrets: the longest range ammo's optimal+falloff*2 + 10%, capped at 300km.
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
                    
                    # Get turret base stats
                    turret_base = getTurretBaseStats(mod)
                    
                    # Check all compatible charges for this turret
                    for charge in mod.getValidCharges():
                        charge_stats = getChargeStats(charge)
                        
                        # Calculate effective optimal + 2*falloff (where DPS drops to ~6%)
                        effective_optimal = turret_base['optimal'] * charge_stats['rangeMultiplier']
                        effective_falloff = turret_base['falloff'] * charge_stats['falloffMultiplier']
                        effective_max = effective_optimal + effective_falloff * 2.5
                        
                        if effective_max > max_range_m:
                            max_range_m = effective_max
                
                elif mod.hardpoint == FittingHardpoint.MISSILE:
                    # For missiles, check ALL compatible charges to find longest range
                    # We need the max range across all ammo types, not just the loaded one
                    # Get flight multipliers from skills/ship (if charge is loaded)
                    flight_mults = getFlightMultipliers(mod)
                    
                    for charge in mod.getValidCharges():
                        base_velocity = charge.getAttribute('maxVelocity') or 0
                        base_explosion_delay = charge.getAttribute('explosionDelay') or 0
                        if base_velocity > 0 and base_explosion_delay > 0:
                            # Apply skill/ship bonuses to flight attributes
                            maxVelocity = base_velocity * flight_mults['maxVelocity']
                            explosionDelay = base_explosion_delay * flight_mults['explosionDelay']
                            # Estimate range: velocity * flight_time
                            flightTime = explosionDelay / 1000
                            estimated_range = maxVelocity * flightTime
                            if estimated_range > max_range_m:
                                max_range_m = estimated_range
        
        if max_range_m <= 0:
            return inputDef.defaultRange
        
        # Add 10% buffer and convert to km
        max_range_km = (max_range_m * 1.05) / 1000
        
        # Cap at 300km (EVE's max lock range)
        max_range_km = min(max_range_km, 300)
        
        # Round to nice number
        max_range_km = int(max_range_km + 0.5)
        
        return (0, max_range_km)

    def _clearInternalCache(self, reason, extraData):
        if reason in (GraphCacheCleanupReason.fitChanged, GraphCacheCleanupReason.fitRemoved):
            self._projectedCache.clearForFit(extraData)
            # Clear ammo caches - both turret and projected since projected depends on source fit
            if hasattr(self, '_ammo_turret_cache'):
                self._ammo_turret_cache = {}
            if hasattr(self, '_ammo_projected_cache'):
                self._ammo_projected_cache = {}
            if hasattr(self, '_ammo_missile_cache'):
                self._ammo_missile_cache = {}
            if hasattr(self, '_ammo_analysis_done'):
                self._ammo_analysis_done = set()
            if hasattr(self, '_ammo_charge_cache'):
                self._ammo_charge_cache = {}
        elif reason == GraphCacheCleanupReason.graphSwitched:
            self._projectedCache.clearAll()
            # Clear all ammo caches
            if hasattr(self, '_ammo_turret_cache'):
                self._ammo_turret_cache = {}
            if hasattr(self, '_ammo_projected_cache'):
                self._ammo_projected_cache = {}
            if hasattr(self, '_ammo_missile_cache'):
                self._ammo_missile_cache = {}
            if hasattr(self, '_ammo_analysis_done'):
                self._ammo_analysis_done = set()
            if hasattr(self, '_ammo_charge_cache'):
                self._ammo_charge_cache = {}

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
        Returns ammo name (str) or None if no cache available.
        
        This uses the currently plotted transitions cache, which was built
        using the current graph settings. We use the most recent cache entry
        for this fit, which should match what's currently displayed.
        """
        # Normalize distance (km to meters)
        distance = self._normalizeValue(value=x, axisSpec=xSpec, src=src, tgt=None)
        if distance is None:
            return None
        
        fit_id = id(src.item)
        
        # Check turret cache - find the most recent cache entry for this fit
        # Since we cache per (fit_id, quality, resists, projected, speed, sig),
        # we need to find the entry that matches current settings
        if hasattr(self, '_ammo_turret_cache') and self._ammo_turret_cache:
            # Build current cache key parameters
            qualityTier = getattr(self, '_ammoQuality', 'all')
            applyProjected = GraphSettings.getInstance().get('ammoOptimalApplyProjected')
            ignoreResists = GraphSettings.getInstance().get('ammoOptimalIgnoreResists')
            tgtResists = None if (ignoreResists or tgt is None) else tgt.getResists()
            tgtSpeed = tgt.getMaxVelocity() if tgt else 0
            tgtSigRadius = tgt.getSigRadius() if tgt else 0
            
            # Try exact cache key match first
            cache_key = (fit_id, qualityTier, tgtResists, applyProjected, tgtSpeed, tgtSigRadius)
            if cache_key in self._ammo_turret_cache:
                turret_cache = self._ammo_turret_cache[cache_key]
                if turret_cache:
                    for group_info in turret_cache.values():
                        transitions = group_info.get('transitions')
                        if transitions:
                            return getAmmoNameAtDistance(transitions, distance)
            
            # Fallback: find any cache entry for this fit (less accurate but better than nothing)
            for cache_key, turret_cache in self._ammo_turret_cache.items():
                if cache_key[0] == fit_id and turret_cache:
                    for group_info in turret_cache.values():
                        transitions = group_info.get('transitions')
                        if transitions:
                            return getAmmoNameAtDistance(transitions, distance)
        
        # Check missile cache with same logic
        if hasattr(self, '_ammo_missile_cache') and self._ammo_missile_cache:
            for cache_key, missile_cache in self._ammo_missile_cache.items():
                if cache_key[0] == fit_id and missile_cache:
                    for group_info in missile_cache.values():
                        transitions = group_info.get('transitions')
                        if transitions:
                            return getAmmoNameAtDistance(transitions, distance)
        
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

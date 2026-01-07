NAVY_PREFIXES = (
    'Imperial Navy ',
    'Republic Fleet ', 
    'Caldari Navy ',
    'Federation Navy '
)

CAPITAL_NAVY_PREFIXES = (
    'Sansha ',
    'Arch Angel ',
    'Shadow '
)


def filterChargesByQuality(charges, qualityTier):
    if qualityTier == 'all':
        return charges
    
    filtered = []
    for charge in charges:
        mg = charge.metaGroup
        mgId = mg.ID if mg else None
        
        if mgId == 1:
            filtered.append(charge)
            continue
        
        if mgId == 2:
            filtered.append(charge)
            continue
        
        if qualityTier == 'navy' and mgId == 4:
            isCapital = charge.name.endswith(' XL')
            
            if isCapital:
                if any(charge.name.startswith(prefix) for prefix in CAPITAL_NAVY_PREFIXES):
                    filtered.append(charge)
            else:
                if any(charge.name.startswith(prefix) for prefix in NAVY_PREFIXES):
                    filtered.append(charge)
    
    return filtered if filtered else charges



def getChargeStats(charge):
    em = charge.getAttribute('emDamage') or 0
    thermal = charge.getAttribute('thermalDamage') or 0
    kinetic = charge.getAttribute('kineticDamage') or 0
    explosive = charge.getAttribute('explosiveDamage') or 0
    
    return {
        'emDamage': em,
        'thermalDamage': thermal,
        'kineticDamage': kinetic,
        'explosiveDamage': explosive,
        'totalDamage': em + thermal + kinetic + explosive,
        'rangeMultiplier': charge.getAttribute('weaponRangeMultiplier') or 1,
        'falloffMultiplier': charge.getAttribute('fallofMultiplier') or 1,
        'trackingMultiplier': charge.getAttribute('trackingSpeedMultiplier') or 1
    }



def applyResists(chargeStats, tgtResists):
    if not tgtResists:
        return chargeStats
    
    emRes, thermRes, kinRes, exploRes = tgtResists
    
    em = chargeStats['emDamage'] * (1 - emRes)
    thermal = chargeStats['thermalDamage'] * (1 - thermRes)
    kinetic = chargeStats['kineticDamage'] * (1 - kinRes)
    explosive = chargeStats['explosiveDamage'] * (1 - exploRes)
    
    result = chargeStats.copy()
    result.update({
        'emDamage': em,
        'thermalDamage': thermal,
        'kineticDamage': kinetic,
        'explosiveDamage': explosive,
        'totalDamage': em + thermal + kinetic + explosive
    })
    return result



def precomputeChargeData(turretBase, charges, skillMult=1.0, tgtResists=None):
    chargeData = []
    
    for charge in charges:
        stats = getChargeStats(charge)
        
        if tgtResists:
            stats = applyResists(stats, tgtResists)
        
        effectiveOptimal = turretBase['optimal'] * stats['rangeMultiplier']
        effectiveFalloff = turretBase['falloff'] * stats['falloffMultiplier']
        effectiveTracking = turretBase['tracking'] * stats['trackingMultiplier']
        
        rawVolley = stats['totalDamage'] * skillMult * turretBase['damageMultiplier']
        
        chargeData.append({
            'name': charge.name,
            'raw_volley': rawVolley,
            'effective_optimal': effectiveOptimal,
            'effective_falloff': effectiveFalloff,
            'effective_tracking': effectiveTracking
        })
    
    return chargeData


def getLongestRangeMultiplier(charges):
    if not charges:
        return 1.0
    
    maxRangeMult = 1.0
    for charge in charges:
        rangeMult = charge.getAttribute('weaponRangeMultiplier') or 1.0
        if rangeMult > maxRangeMult:
            maxRangeMult = rangeMult
    
    return maxRangeMult

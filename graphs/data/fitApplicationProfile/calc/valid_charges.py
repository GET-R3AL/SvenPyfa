import eos.db
from eos.gamedata import Item


_validChargesCache = {}


def getValidChargesForModule(module):
    if module.item.ID in _validChargesCache:
        return _validChargesCache[module.item.ID].copy()
    
    chargeGroupIDs = []
    for i in range(5):
        itemChargeGroup = module.getModifiedItemAttr('chargeGroup' + str(i), None)
        if itemChargeGroup:
            chargeGroupIDs.append(int(itemChargeGroup))
    
    if not chargeGroupIDs:
        _validChargesCache[module.item.ID] = set()
        return set()
    
    session = eos.db.get_gamedata_session()
    
    items = session.query(Item).filter(
        Item.groupID.in_(chargeGroupIDs),
        Item.published == True
    ).all()
    
    for item in items:
        if module.isValidCharge(item):
            validCharges.add(item)
    
    _validChargesCache[module.item.ID] = validCharges
    return validCharges.copy()


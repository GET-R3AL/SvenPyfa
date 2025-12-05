"""Test script to examine charge compatibility and ordering for Tachyon Beam Laser II"""
import os
import sys

# Setup paths
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import eos.db
from eos.saveddata.fit import Fit
from eos.saveddata.ship import Ship
from eos.saveddata.module import Module

# Create a fit to get a proper module
ship_item = eos.db.getItem("Apocalypse")
ship = Ship(ship_item)
fit = Fit(ship)

# Create the module
tachyon_item = eos.db.getItem("Tachyon Beam Laser II")
mod = Module(tachyon_item)
fit.modules.append(mod)
fit.calculateModifiedAttributes()

print(f"Module: {mod.item.name} (ID: {mod.item.ID})")
print(f"Group: {mod.item.group.name}")
print()

# Get compatible charges
charges = list(mod.getValidCharges())
print(f"Total compatible charges: {len(charges)}")
print()

# Print all charges with their properties - DEFAULT ORDER
print("=" * 100)
print("DEFAULT ORDER FROM getValidCharges():")
print("=" * 100)
print(f"{'#':<4} {'Name':<40} {'ID':>8} {'Meta':>6} {'MetaGrp':>8}")
print("-" * 100)

for i, charge in enumerate(charges):
    meta = getattr(charge, 'metaLevel', None) or 0
    meta_grp = getattr(charge, 'metaGroupID', None) or 0
    print(f"{i:<4} {charge.name:<40} {charge.ID:>8} {meta:>6} {meta_grp:>8}")

print()
print("=" * 100)
print("SORTED BY ID:")
print("=" * 100)
for charge in sorted(charges, key=lambda c: c.ID):
    meta = getattr(charge, 'metaLevel', None) or 0
    print(f"{charge.name:<40} {charge.ID:>8} {meta:>6}")

print()
print("=" * 100)
print("SORTED BY META LEVEL:")
print("=" * 100)
for charge in sorted(charges, key=lambda c: (getattr(c, 'metaLevel', 0) or 0, c.ID)):
    meta = getattr(charge, 'metaLevel', None) or 0
    print(f"{charge.name:<40} {charge.ID:>8} {meta:>6}")

print()
print("=" * 100)
print("EXAMINING KEY CHARGE ATTRIBUTES:")
print("=" * 100)

# Get a sample of different ammo types
sample_names = ['Multifrequency L', 'Gleam L', 'Aurora L', 'Imperial Navy Multifrequency L', 'Conflagration L']
for name in sample_names:
    for charge in charges:
        if charge.name == name:
            print(f"\n{charge.name} (ID: {charge.ID}):")
            # Get key attributes
            attrs_of_interest = ['emDamage', 'thermalDamage', 'kineticDamage', 'explosiveDamage', 
                                 'trackingSpeedMultiplier', 'fallofMultiplier', 'weaponRangeMultiplier',
                                 'metaLevel', 'metaGroupID']
            for attr_name in attrs_of_interest:
                val = getattr(charge, attr_name, None)
                if val is not None:
                    print(f"  {attr_name}: {val}")
            break

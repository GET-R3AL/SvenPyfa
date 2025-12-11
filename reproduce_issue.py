
import logging
from logbook import StreamHandler
import sys
import os

# Setup basic logging to see pyfa logs
StreamHandler(sys.stdout).push_application()

try:
    import eos.db
    from eos.saveddata.fit import Fit
    from eos.saveddata.module import Module
    from eos.const import FittingSlot
except ImportError:
    print("Could not import eos. Make sure you are running from the source root.")
    sys.exit(1)

def test_programmatic_charge_load():
    print("Initializing eos db...")
    
    # Needs a char. If getAll0 fails/returns empty, we might need to handle it.
    from eos.saveddata.character import Character
    # Assuming standard pyfa setup has "All 5"
    # Try getting "All 5" character. getAll5() is likely a static method returning that char.
    try:
        char = Character.getAll5()
        print(f"Using character: {char.name}")
    except AttributeError:
        # Fallback to getAll0 if getAll5 doesn't exist
        char = Character.getAll0()
        print(f"Using character (fallback): {char.name}")
    
    if not char:
        print("No character returned.")
        return

    # 1. Create a simple fit: Ship + Launcher
    # Kestrel (ID 602)
    from eos.saveddata.ship import Ship
    ship_item = eos.db.getItem(602)
    if not ship_item:
        print("Could not find ship Kestrel (602)")
        return
    
    # Needs to be a Ship object, not just an Item
    # Ship(item, owner=None)
    ship_obj = Ship(ship_item) 

    fit = Fit(ship_obj, name="Test Fit")
    fit.character = char
    print(f"Fit created with char: {fit.character.name}")
    
    # Light Missile Launcher II (ID 2407)
    launcher_item = eos.db.getItem(2407) 
    if not launcher_item:
        print("Could not find Launcher (2407)")
        return
        
    mod = Module(launcher_item)
    fit.modules.append(mod)
    
    # Ensure launcher is empty initially
    mod.charge = None
    
    # Force initial calculation
    fit.calculated = False
    
    print(f"Launcher loaded: {mod.item.name}")
    print(f"Initial charge: {mod.charge}")
    
    # 2. Programmatically load charge
    # Mjolnir Light Missile (ID 2516)
    charge_item = eos.db.getItem(2516) 
    if not charge_item:
        print("Could not find Charge (2516)")
        return
        
    print(f"Loading charge: {charge_item.name}")
    
    # --- ACTION UNDER TEST ---
    mod.charge = charge_item
    fit.calculated = False
    # -------------------------
    
    # 3. Check for skill application
    
    # Mjolnir Light (2516) has EM damage.
    base_em = mod.getChargeBaseAttrValue('emDamage')
    modified_em = mod.getModifiedChargeAttr('emDamage')
    
    print(f"Base EM: {base_em}")
    print(f"Modified EM: {modified_em}")
    
    skill_bonus_found = False
    
    if base_em > 0:
        mult = modified_em / base_em
        print(f"Multiplier: {mult}")
        if mult > 1.05: # Expecting significant bonus > 5%
            print("SUCCESS: Modifiers applied! (> 1.05)")
            skill_bonus_found = True
        elif mult > 1.0:
            print("PARTIAL SUCCESS: Modifiers applied but small (> 1.0)")
            skill_bonus_found = True
        else:
            print("FAILURE: Multiplier is 1.0 (or less).")
    
    # Check flight time / velocity (missile projection skill)
    base_vel = mod.getChargeBaseAttrValue('maxVelocity')
    mod_vel = mod.getModifiedChargeAttr('maxVelocity')
    print(f"Base Velocity: {base_vel}")
    print(f"Modified Velocity: {mod_vel}")
    if base_vel > 0:
         mult_vel = mod_vel / base_vel
         print(f"Velocity Multiplier: {mult_vel}")
         if mult_vel > 1.0:
             print("Velocity bonus applied.")
         else:
             print("Velocity bonus NOT applied.")

if __name__ == "__main__":
    test_programmatic_charge_load()

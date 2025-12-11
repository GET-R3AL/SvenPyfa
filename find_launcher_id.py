
import logging
from logbook import StreamHandler
import sys

StreamHandler(sys.stdout).push_application()

try:
    import eos.db
except ImportError:
    pass

def find_launcher():
    # Simple search if possible. eos.db doesn't expose search easily in public API maybe?
    # Let's try iterating valid items? No that's too slow.
    # We can try a few common IDs.
    
    ids_to_try = [
        2407, # Light Missile Launcher II
        499, # Missile Launcher I
        2217, # Arbalest Standard Missile Launcher
        2516, # Charge, not launcher
    ]
    
    for i in ids_to_try:
        item = eos.db.getItem(i)
        if item:
            print(f"ID {i}: {item.name}")
        else:
            print(f"ID {i}: Not found")

    # If all else fails, use a raw SQL query if we can?
    # eos.db.gamedata is a scoped session
    try:
        res = eos.db.gamedata.execute("SELECT itemID, typeName FROM invTypes WHERE typeName LIKE '%Missile Launcher II%' LIMIT 5")
        for row in res:
            print(f"Found: {row[0]} - {row[1]}")
    except Exception as e:
        print(f"SQL failed: {e}")

if __name__ == "__main__":
    find_launcher()

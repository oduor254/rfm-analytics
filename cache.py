"""
Helper script to create RFM cache from retention dashboard data
Run this ONCE to avoid Google Sheets timeout on first RFM load
"""

import pandas as pd
import pickle

print("="*60)
print("RFM Cache Creator")
print("="*60)

# Option 1: If your retention dashboard has cached data
try:
    # Try to import from retention app
    import sys
    sys.path.insert(0, '.')
    
    from app import get_customer_data
    print("[INFO] Loading data from retention dashboard...")
    
    df = get_customer_data()
    
    if df is not None and len(df) > 0:
        # Save to RFM cache
        df.to_pickle('rfm_data_cache.pkl')
        print(f"[SUCCESS] Saved {len(df)} records to rfm_data_cache.pkl")
        print("[INFO] RFM dashboard can now use this cache!")
        print("\nYou can now run: python rfm.py")
    else:
        print("[ERROR] No data available from retention dashboard")
        
except Exception as e:
    print(f"[ERROR] Could not load from retention: {e}")
    print("\nAlternative: Run your retention dashboard first (python app.py)")
    print("Then run this script again.")

print("="*60)
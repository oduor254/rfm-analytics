import certifi
import ssl
import os
from dotenv import load_dotenv
load_dotenv()

# Set environment variables for SSL certificates
os.environ['REQUESTS_CA_BUNDLE'] = certifi.where()
os.environ['CURL_CA_BUNDLE'] = certifi.where()

# Suppress SSL warnings if needed
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from flask import Flask, render_template, jsonify, request
import gspread
from google.oauth2.service_account import Credentials
import pandas as pd
import numpy as np
from datetime import datetime
import json
import base64
import time
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter
import requests
from datetime import datetime, timedelta

# Templates resolved relative to this file so Vercel finds them correctly
_template_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates')
app = Flask(__name__, template_folder=_template_dir)

# Configuration — JSON_FILE_PATH is the local fallback; production uses GOOGLE_CREDENTIALS_JSON env var
JSON_FILE_PATH = r'C:\Users\Oduor\Downloads\JSON Files\retention-484110-9e4520124486.json'
SHEET_NAME = 'Customer Database'
WORKSHEET_NAME = 'Shops'

# Cache variables
cached_data = None
last_fetch_time = None
CACHE_DURATION = 300  # Cache for 5 minutes

# RFM Segment Weights - Monetary gets highest priority
# Weighted score = (R * 0.20) + (F * 0.20) + (M * 0.60)
RFM_WEIGHTS = {'R': 0.20, 'F': 0.20, 'M': 0.60}

# Customer of the Month awarding criteria per shop
# min_spend: minimum monthly spend to qualify (KSh)
# award: bag prize value (KSh), None = TBD
# status: 'active' = threshold-based, 'verify' = manual check, 'monitoring' = observation only
AWARDING_CRITERIA = {
    'Hilton':     {'min_spend': 50000, 'award': 4000,  'status': 'active'},
    'Starmall':   {'min_spend': 30000, 'award': 3000,  'status': 'active'},
    'Ktda':       {'min_spend': 20000, 'award': 2000,  'status': 'active'},
    'Hazina':     {'min_spend': 20000, 'award': 2000,  'status': 'active'},
    'Kakamega':   {'min_spend': 20000, 'award': 2000,  'status': 'active'},
    'Kisii':      {'min_spend': 20000, 'award': 2000,  'status': 'active'},
    'Mombasa':    {'min_spend': 15000, 'award': 2000,  'status': 'active'},
    'Nakuru':     {'min_spend': 15000, 'award': 2000,  'status': 'active'},
    'Eldoret':    {'min_spend': 15000, 'award': 2000,  'status': 'active'},
    'Thika':      {'min_spend': 15000, 'award': 2000,  'status': 'active'},
    'Kisumu':     {'min_spend': 15000, 'award': 2000,  'status': 'active'},
    'Meru':       {'min_spend': 15000, 'award': 2000,  'status': 'active'},
    'Kitengela':  {'min_spend': 15000, 'award': 2000,  'status': 'active'},
    'Nanyuki':    {'min_spend': 10000, 'award': 1200,  'status': 'active'},
    'Busia':      {'min_spend': 10000, 'award': 1200,  'status': 'active'},
    'Corporate':  {'min_spend': None,  'award': None,  'status': 'verify'},
    'Website':    {'min_spend': None,  'award': None,  'status': 'verify'},
    'Rongai':     {'min_spend': None,  'award': None,  'status': 'monitoring'},
    'Sinza':      {'min_spend': None,  'award': None,  'status': 'monitoring'},
    'Uganda':     {'min_spend': None,  'award': None,  'status': 'monitoring'},
}

def assign_rfm_segment(r_score, f_score, m_score):
    """
    Assign RFM segment using MONETARY-FIRST logic.
    
    Monetary is the primary driver (60% weight). A high spender with
    20-day recency in a month still qualifies as Champions/Big Spenders
    because their revenue contribution outweighs recency concerns.
    
    Weighted Score = (R * 0.20) + (F * 0.20) + (M * 0.60)
    """
    weighted_score = (r_score * RFM_WEIGHTS['R'] + 
                      f_score * RFM_WEIGHTS['F'] + 
                      m_score * RFM_WEIGHTS['M'])
    
    # --- MONETARY-FIRST RULES (checked first, override everything) ---
    
    # Rule 1: Top spenders (M=5) are Champions or Big Spenders regardless of R
    if m_score == 5:
        if f_score >= 4:  # High spend + high frequency = Champion
            return 'Champions'
        elif f_score >= 2:  # High spend + moderate frequency = Big Spender
            return 'Big Spenders'
        else:  # High spend + low frequency = still Big Spender (one-time big buyer)
            return 'Big Spenders'
    
    # Rule 2: High spenders (M=4) get premium segments even with weaker R
    if m_score == 4:
        if f_score >= 4 and r_score >= 3:  # Good frequency + decent recency
            return 'Champions'
        elif f_score >= 3 or r_score >= 4:  # Either good frequency or great recency
            return 'Big Spenders'
        elif r_score >= 2:  # Moderate recency but still high spend
            return 'Loyal Customers'
        else:  # Very old but high spend historically
            return 'At Risk'
    
    # Rule 3: Moderate spenders (M=3) 
    if m_score == 3:
        if r_score >= 4 and f_score >= 3:
            return 'Loyal Customers'
        elif r_score >= 4 and f_score <= 2:
            return 'New Customers'  # Recent, low frequency, moderate spend
        elif r_score >= 3:
            return 'Loyal Customers'
        elif r_score >= 2:
            return 'At Risk'
        else:
            return 'Lost'
    
    # Rule 4: Low spenders (M=2)
    if m_score == 2:
        if r_score >= 4 and f_score >= 3:
            return 'Loyal Customers'  # Frequent + recent, just low ticket
        elif r_score >= 4:
            return 'New Customers'
        elif r_score >= 3:
            return 'At Risk'
        else:
            return 'Lost'
    
    # Rule 5: Very low spenders (M=1)
    if m_score == 1:
        if r_score >= 4 and f_score >= 3:
            return 'New Customers'  # Recent & frequent but tiny spend
        elif r_score >= 4:
            return 'New Customers'
        elif r_score >= 3:
            return 'Very Low Customers'
        else:
            return 'Very Low Customers'
    
    # Fallback based on weighted score
    if weighted_score >= 4.0:
        return 'Champions'
    elif weighted_score >= 3.4:
        return 'Big Spenders'
    elif weighted_score >= 2.8:
        return 'Loyal Customers'
    elif weighted_score >= 2.2:
        return 'At Risk'
    elif weighted_score >= 1.6:
        return 'Lost'
    else:
        return 'Very Low Customers'

def get_customer_data():
    """Fetch and process customer data - with smart caching"""
    global cached_data, last_fetch_time
    
    # ALWAYS return cached data if available (don't auto-refresh on timeout)
    if cached_data is not None:
        cache_age = time.time() - last_fetch_time if last_fetch_time else 999999
        if cache_age < CACHE_DURATION:
            print(f"[DEBUG] Returning fresh cached data (age: {int(cache_age)}s)")
            return cached_data
        else:
            print(f"[DEBUG] Cache is stale ({int(cache_age)}s old), but will try refresh")
    
    # Try to fetch fresh data
    try:
        print("[INFO] Fetching data from Google Sheets...")
        SCOPES = ['https://www.googleapis.com/auth/spreadsheets', 
                  'https://www.googleapis.com/auth/drive']
        
        creds_env = os.environ.get('GOOGLE_CREDENTIALS_JSON')
        if creds_env:
            creds_info = json.loads(base64.b64decode(creds_env).decode('utf-8'))
            creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
        else:
            creds = Credentials.from_service_account_file(JSON_FILE_PATH, scopes=SCOPES)
        
        # Create client with timeout settings
        client = gspread.authorize(creds)
        client.set_timeout(15)  # Shorter timeout
        
        spreadsheet = client.open(SHEET_NAME)
        worksheet = spreadsheet.worksheet(WORKSHEET_NAME)
        
        data = worksheet.get_all_records()
        df = pd.DataFrame(data)

        # Handle column naming - STANDARDIZE TO 'Shop'
        if 'Location' in df.columns:
            print("[INFO] Renaming 'Location' column to 'Shop'")
            df.rename(columns={'Location': 'Shop'}, inplace=True)
            
        if 'Gender' not in df.columns and 'Female' in df.columns:
            print("[INFO] Renaming 'Female' column to 'Gender'")
            df.rename(columns={'Female': 'Gender'}, inplace=True)
            
        if 'Shop' in df.columns:
            df['Shop'] = df['Shop'].astype(str).str.strip().str.title()
            
        if df.empty:
            raise ValueError("No data returned from Google Sheets")
        
        print(f"[INFO] Loaded {len(df)} records from Google Sheets")
        
        df['Date'] = pd.to_datetime(df['Date'], errors='coerce')
        
        # Filter out organizations and only 2025-2026 data
        print(f"[DEBUG] Raw data records before filtering: {len(df)}")
        print(f"[DEBUG] Records with null Gender: {df['Gender'].isna().sum()}")
        print(f"[DEBUG] Records with 'organization' gender: {(df['Gender'].str.lower().str.strip() == 'organization').sum()}")
        print(f"[DEBUG] Records before year filter: {len(df[df['Gender'].str.lower().str.strip() != 'organization'])}")
        print(f"[DEBUG] Records with year < 2025: {(df['Date'].dt.year < 2025).sum()}")
        print(f"[DEBUG] Records with null Date: {df['Date'].isna().sum()}")
        
        df_filtered = df[
            (df['Gender'].str.lower().str.strip() != 'organization') & 
            (df['Date'].dt.year >= 2025)
        ].copy()
        
        print(f"[DEBUG] Data after filtering out organization AND year < 2025: {len(df_filtered)} records")
        print(f"[DEBUG] Records FILTERED OUT: {len(df) - len(df_filtered)}")
        
        if df_filtered.empty:
            raise ValueError("No data matches the filter criteria")
        
        # Clean price data
        df_filtered['Price'] = df_filtered['Price'].astype(str).str.replace(r'[^\d.]', '', regex=True)
        df_filtered['Price'] = pd.to_numeric(df_filtered['Price'], errors='coerce')
        
        print(f"[DEBUG] Records with null Price after conversion: {df_filtered['Price'].isna().sum()}")
        print(f"[DEBUG] Records with Price = 0: {(df_filtered['Price'] == 0).sum()}")
        print(f"[DEBUG] Records with null First Name: {df_filtered['First Name'].isna().sum()}")
        print(f"[DEBUG] Records with null Phone: {df_filtered['Phone'].isna().sum()}")
        
        # Create customer identifier - use Phone only (matches Excel pivot table logic)
        df_filtered['Customer_ID'] = df_filtered['Phone'].astype(str).str.strip()
        
        print(f"[DEBUG] Total records after all processing: {len(df_filtered)}")
        print(f"[DEBUG] Unique customers after filtering: {df_filtered['Customer_ID'].nunique()}")
        print(f"[DEBUG] Records with null/empty Customer_ID: {(df_filtered['Customer_ID'].isna() | (df_filtered['Customer_ID'] == '')).sum()}")
        
        print(f"[DEBUG] Unique Customer_IDs: {df_filtered['Customer_ID'].nunique()}")
        print(f"[DEBUG] Total records: {len(df_filtered)}")
        print(f"[DEBUG] Customer_ID duplicates: {len(df_filtered) - df_filtered['Customer_ID'].nunique()}")
        
        # Save to cache
        cached_data = df_filtered
        last_fetch_time = time.time()
        
        # Save to local backup file
        try:
            df_filtered.to_pickle('rfm_data_cache.pkl')
            print("[INFO] Saved data to local cache file")
        except:
            pass
        
        return df_filtered
    
    except requests.exceptions.Timeout:
        print("[ERROR] Connection timed out to Google Sheets.")
        # Try to return stale cache or load from file
        if cached_data is not None:
            print("[INFO] Using stale cache due to timeout")
            return cached_data
        # Try loading from backup file
        try:
            df = pd.read_pickle('rfm_data_cache.pkl')
            print("[INFO] Loaded data from local backup file")
            cached_data = df
            last_fetch_time = time.time() - CACHE_DURATION + 60  # Mark as "almost stale"
            return df
        except:
            raise Exception("Timeout connecting to Google Sheets and no local cache available. Please try again.")
    
    except Exception as e:
        print(f"[ERROR] Connection failed: {str(e)}")
        # Try stale cache
        if cached_data is not None:
            print("[INFO] Using stale cache due to error")
            return cached_data
        # Try backup file
        try:
            df = pd.read_pickle('rfm_data_cache.pkl')
            print("[INFO] Loaded data from local backup file")
            cached_data = df
            last_fetch_time = time.time() - CACHE_DURATION + 60
            return df
        except:
            raise Exception(f"Error fetching customer data: {str(e)}")

def get_period_type(period_str):
    """Determine the period type from the period string"""
    if not period_str or period_str == "Overall":
        return "yearly"  # Default to yearly for overall
    elif period_str.startswith("Q"):  # Q1 2025, Q2 2025, etc.
        return "quarterly"
    elif period_str.startswith("H"):  # H1 2025, H2 2025
        return "semi_annual"
    elif len(period_str) == 4 and period_str.isdigit():  # 2025, 2026
        return "yearly"
    else:  # January 2025, February 2025, etc.
        return "monthly"

def get_analysis_date(period, raw_df):
    """Calculate analysis date based on the selected period"""
    if period == "Overall":
        return raw_df['Date'].max()
    elif period.startswith("Q"):  # Q1 2025
        q, year = period.split(' ')
        month_map = {"Q1": 3, "Q2": 6, "Q3": 9, "Q4": 12}
        month = month_map[q]
        # Get last day of the quarter
        return pd.to_datetime(f"{year}-{month:02d}") + pd.offsets.QuarterEnd(0)
    elif period.startswith("H"):  # H1 2025
        h, year = period.split(' ')
        month = 6 if h == "H1" else 12
        # Get last day of the half-year
        return pd.to_datetime(f"{year}-{month:02d}") + pd.offsets.SemiMonthEnd(0) if month == 6 else pd.to_datetime(f"{year}-12-31")
    elif len(period) == 4 and period.isdigit():  # 2025
        return pd.to_datetime(f"{period}-12-31")
    else:  # Monthly: "January 2025"
        try:
            target_date = pd.to_datetime(period)
            return target_date + pd.offsets.MonthEnd(0)
        except:
            return raw_df['Date'].max()

def get_rfm_thresholds(period_type):
    """
    Get period-specific RFM thresholds
    Returns dict with 'recency', 'frequency', and 'monetary' thresholds
    """
    thresholds = {
        'monthly': {
            'recency': {  # Days since last purchase (for 1 month period)
                5: 7,      # Within 1 week = excellent
                4: 14,     # Within 2 weeks = good
                3: 21,     # Within 3 weeks = average
                2: 29,     # Within the month = poor
                1: 30     # Beyond = very poor
            },
            'frequency': {  # Number of purchases in 1 month
                1: 2,      # 2 purchase
                2: 4,      # 4 purchases
                3: 7,      # 7 purchases
                4: 10,      # 10 purchases
                5:15     # 15+ purchases
            },
            'monetary': {  # Total spend in 1 month
                1: 1000,   
                2: 5000,   
                3: 10000,   
                4: 18000,   
                5: 20000  
            }
        },
        'quarterly': {
            'recency': {  # Days since last purchase (for 3 month period)
                5: 21,     # Within 3 weeks = excellent
                4: 45,     # Within 1.5 months = good
                3: 60,     # Within 2 months = average
                2: 80,     # Within the quarter = poor
                1: 85     # Beyond = very poor
            },
            'frequency': {  # Number of purchases in 3 months
                1: 2,      # 1-2 purchases
                2: 5,      # 3-5 purchases
                3: 10,      # 5-10 purchases
                4: 15,     # 11-15 purchases
                5: 16     # 16+ purchases
            },
            'monetary': {  # Total spend in 3 months
                1: 3000,   
                2: 5000,   
                3: 12000,  
                4: 20000,  
                5: 30000  
            }
        },
        'semi_annual': {
            'recency': {  # Days since last purchase (for 6 month period)
                5: 30,     # Within 1 month = excellent
                4: 60,     # Within 2 months = good
                3: 120,    # Within 4 months = average
                2: 180,    # Within 6 months = poor
                1: 185     # Beyond = very poor
            },
            'frequency': {  # Number of purchases in 6 months
                1: 3,      # 1-3 purchases
                2: 8,      # 4-8 purchases
                3: 12,     # 9-12 purchases
                4: 15,     # 13-15 purchases
                5: 20     # 20+ purchases
            },
            'monetary': {  # Total spend in 6 months
                1: 5000,   
                2: 12000,  
                3: 20000,  
                4: 25000,  
                5: 40000  
            }
        },
        'yearly': {
            'recency': {  # Days since last purchase (for 12 month period)
                5: 30,     # Within 1 month = excellent
                4: 60,     # Within 2 months = good
                3: 120,    # Within 4 months = average
                2: 240,    # Within 8 months = poor
                1: 365     # Beyond = very poor
            },
            'frequency': {  # Number of purchases in 12 months
                1: 4,      # 1-4 purchases
                2: 8,      # 5-8 purchases
                3: 15,     # 9-15 purchases
                4: 25,     # 16-25 purchases
                5: 26     # 26+ purchases
            },
            'monetary': {  # Total spend in 12 months
                1: 8000,   
                2: 18000,  
                3: 35000,  
                4: 50000,  
                5: 55000  
            }
        }
    }
    
    return thresholds.get(period_type, thresholds['yearly'])

def calculate_rfm_scores(df, analysis_date=None, period_type='yearly'):
    """
    Calculate RFM scores for all customers with period-specific thresholds
    
    Args:
        df: DataFrame with customer transaction data
        analysis_date: Date to calculate recency from (default: max date in df)
        period_type: Type of period ('monthly', 'quarterly', 'semi_annual', 'yearly')
    """
    if analysis_date is None:
        analysis_date = df['Date'].max()
    
    # Customer summary - using 'Shop' column
    # First, filter out zero prices for revenue calculation
    df_for_revenue = df[df['Price'] > 0].copy()
    df_for_count = df.copy()  # Keep all records for order counting
    
    customer_summary = df_for_count.groupby('Customer_ID').agg({
        'First Name': 'first',
        'Phone': 'first',
        'Gender': 'first',
        'Shop': 'first',  # ✅ Using 'Shop' here
        'Date': ['min', 'max', 'count']
    }).reset_index()
    
    # Now add revenue from non-zero prices only
    revenue_by_customer = df_for_revenue.groupby('Customer_ID')['Price'].sum().to_dict()
    customer_summary['Total_Revenue'] = customer_summary['Customer_ID'].map(revenue_by_customer).fillna(0)
    
    # ✅ Keep it as 'Shop' in the column names
    customer_summary.columns = ['Customer_ID', 'First_Name', 'Phone', 'Gender', 'Shop', 
                                 'First_Purchase_Date', 'Last_Purchase_Date', 'Total_Orders', 'Total_Revenue']
    
    # Calculate Days_Active
    customer_summary['Days_Active'] = (customer_summary['Last_Purchase_Date'] - 
                                       customer_summary['First_Purchase_Date']).dt.days
    
    # Calculate Purchase_Frequency (per month)
    customer_summary['Purchase_Frequency'] = customer_summary.apply(
        lambda row: row['Total_Orders'] / (row['Days_Active'] / 30) if row['Days_Active'] > 0 else row['Total_Orders'],
        axis=1
    )
    customer_summary['Purchase_Frequency'] = customer_summary['Purchase_Frequency'].replace([np.inf, -np.inf], 0).fillna(0)
    
    # Calculate Recency (days since last purchase)
    customer_summary['Recency_Days'] = (analysis_date - customer_summary['Last_Purchase_Date']).dt.days
    customer_summary['Frequency'] = customer_summary['Total_Orders']
    customer_summary['Monetary'] = customer_summary['Total_Revenue']
    
    print(f"[DEBUG] Customer summary Monetary values (first 5): {customer_summary['Monetary'].head().tolist()}")
    print(f"[DEBUG] Customer summary Monetary mean: {customer_summary['Monetary'].mean()}")
    print(f"[DEBUG] Customer summary Monetary sum: {customer_summary['Monetary'].sum()}")
    print(f"[DEBUG] Customer summary Monetary count (unique customers): {len(customer_summary)}")
    print(f"[DEBUG] Total Revenue calculation check:")
    print(f"[DEBUG]   - Records with Price > 0: {len(df_for_revenue)}")
    print(f"[DEBUG]   - Sum of prices (non-zero): {df_for_revenue['Price'].sum()}")
    print(f"[DEBUG]   - Expected avg = {df_for_revenue['Price'].sum()} / {len(customer_summary)} = {df_for_revenue['Price'].sum() / len(customer_summary) if len(customer_summary) > 0 else 0}")
    
    # Get period-specific thresholds
    thresholds = get_rfm_thresholds(period_type)
    print(f"[INFO] Using {period_type} RFM thresholds")
    
    # Calculate R, F, M scores with period-specific thresholds
    def calculate_r_score(recency):
        """Recency score: Lower recency (more recent) = Higher score"""
        if recency < 0:
            return 0
        r_thresholds = thresholds['recency']
        if recency <= r_thresholds[5]:
            return 5
        elif recency <= r_thresholds[4]:
            return 4
        elif recency <= r_thresholds[3]:
            return 3
        elif recency <= r_thresholds[2]:
            return 2
        else:
            return 1
    
    def calculate_f_score(frequency):
        """Frequency score: More purchases = Higher score"""
        f_thresholds = thresholds['frequency']
        if frequency <= f_thresholds[1]:
            return 1
        elif frequency <= f_thresholds[2]:
            return 2
        elif frequency <= f_thresholds[3]:
            return 3
        elif frequency <= f_thresholds[4]:
            return 4
        else:
            return 5
    
    def calculate_m_score(monetary):
        """Monetary score: Higher spend = Higher score"""
        m_thresholds = thresholds['monetary']
        if monetary <= m_thresholds[1]:
            return 1
        elif monetary <= m_thresholds[2]:
            return 2
        elif monetary <= m_thresholds[3]:
            return 3
        elif monetary <= m_thresholds[4]:
            return 4
        else:
            return 5
    
    customer_summary['R_Score'] = customer_summary['Recency_Days'].apply(calculate_r_score)
    customer_summary['F_Score'] = customer_summary['Frequency'].apply(calculate_f_score)
    customer_summary['M_Score'] = customer_summary['Monetary'].apply(calculate_m_score)
    
    # Combine RFM scores (kept for display/debugging)
    customer_summary['RFM_Combined'] = (customer_summary['R_Score'].astype(str) + 
                                        customer_summary['F_Score'].astype(str) + 
                                        customer_summary['M_Score'].astype(str))
    
    # Calculate weighted RFM score (Monetary = 60%, Recency = 20%, Frequency = 20%)
    customer_summary['RFM_Weighted_Score'] = (
        customer_summary['R_Score'] * RFM_WEIGHTS['R'] + 
        customer_summary['F_Score'] * RFM_WEIGHTS['F'] + 
        customer_summary['M_Score'] * RFM_WEIGHTS['M']
    ).round(2)
    
    # Assign segments using monetary-first logic
    customer_summary['RFM_Segment'] = customer_summary.apply(
        lambda row: assign_rfm_segment(row['R_Score'], row['F_Score'], row['M_Score']),
        axis=1
    )
    
    print(f"[INFO] RFM Weighted Score distribution:")
    print(f"[INFO]   Mean: {customer_summary['RFM_Weighted_Score'].mean():.2f}")
    print(f"[INFO]   Segments: {customer_summary['RFM_Segment'].value_counts().to_dict()}")
    
    return customer_summary

def calculate_segment_distribution(rfm_df):
    """Calculate segment distribution"""
    segment_counts = rfm_df['RFM_Segment'].value_counts().to_dict()
    segment_total = len(rfm_df)
    
    segments = []
    for segment, count in segment_counts.items():
        percentage = (count / segment_total * 100) if segment_total > 0 else 0
        avg_revenue = rfm_df[rfm_df['RFM_Segment'] == segment]['Monetary'].mean()
        avg_frequency = rfm_df[rfm_df['RFM_Segment'] == segment]['Frequency'].mean()
        
        segments.append({
            'segment': segment,
            'count': int(count),
            'percentage': round(float(percentage), 2),
            'avg_revenue': round(float(avg_revenue), 2) if not np.isnan(avg_revenue) else 0,
            'avg_frequency': round(float(avg_frequency), 2) if not np.isnan(avg_frequency) else 0
        })
    
    return sorted(segments, key=lambda x: x['count'], reverse=True)

def calculate_purchase_frequency_distribution(rfm_df):
    """Calculate purchase frequency distribution"""
    freq_bins = [0, 2, 4, 7, 10, float('inf')]
    freq_labels = ['1-2 purchases', '3-4 purchases', '5-7 purchases', '8-10 purchases', '10+ purchases']
    
    rfm_df['Frequency_Bin'] = pd.cut(rfm_df['Frequency'], bins=freq_bins, labels=freq_labels, include_lowest=True)
    freq_dist = rfm_df['Frequency_Bin'].value_counts().to_dict()
    
    distribution = []
    total = len(rfm_df)
    for label in freq_labels:
        count = freq_dist.get(label, 0)
        percentage = (count / total * 100) if total > 0 else 0
        distribution.append({
            'range': label,
            'count': int(count),
            'percentage': round(float(percentage), 2)
        })
    
    return distribution

def calculate_shop_gender_distribution(rfm_df):
    """Calculate customer distribution by shop and gender"""
    # ✅ Group by 'Shop' instead of 'Location'
    shop_gender = rfm_df.groupby(['Shop', 'Gender']).size().unstack(fill_value=0)
    
    shops = []
    for shop_name in shop_gender.index:
        male_count = int(shop_gender.loc[shop_name].get('Male', 0))
        female_count = int(shop_gender.loc[shop_name].get('Female', 0))
        total = male_count + female_count
        
        shops.append({
            'shop': shop_name,
            'male_count': male_count,
            'male_percentage': round(float((male_count / total * 100) if total > 0 else 0), 2),
            'female_count': female_count,
            'female_percentage': round(float((female_count / total * 100) if total > 0 else 0), 2),
            'total': total
        })
    
    return sorted(shops, key=lambda x: x['total'], reverse=True)

def calculate_spending_analysis(filtered_transactions_df, rfm_df):
    """Calculate average spend and distribution
    
    Args:
        filtered_transactions_df: Raw transaction data filtered to the selected period/shop
        rfm_df: RFM-scored customer data (already filtered to period/shop)
    """
    avg_spend = rfm_df['Monetary'].mean()  # Lifetime value (already excludes zeros from RFM calc)
    median_spend = rfm_df['Monetary'].median()
    
    print(f"[DEBUG] Spending Analysis:")
    print(f"[DEBUG]   - RFM Monetary mean (avg_spend): {avg_spend}")
    print(f"[DEBUG]   - RFM Monetary sum (total revenue): {rfm_df['Monetary'].sum()}")
    print(f"[DEBUG]   - Unique customers in RFM: {len(rfm_df)}")
    print(f"[DEBUG]   - Calculation: {rfm_df['Monetary'].sum()} / {len(rfm_df)} = {avg_spend}")
    
    # Average spend per visit - IMPORTANT: Exclude zero prices (combo items)
    df_work = filtered_transactions_df[filtered_transactions_df['Price'] > 0].copy()
    df_work['Visit_Date'] = df_work['Date'].dt.date
    visit_spending = df_work.groupby(['Customer_ID', 'Visit_Date'])['Price'].sum().reset_index()
    avg_spend_per_visit = visit_spending['Price'].mean() if len(visit_spending) > 0 else 0
    total_visits = len(visit_spending)
    
    print(f"[DEBUG]   - Avg spend per visit: {avg_spend_per_visit}")
    print(f"[DEBUG]   - Total visits: {total_visits}")
    
    below_avg = int((rfm_df['Monetary'] < avg_spend).sum())
    above_avg = int((rfm_df['Monetary'] >= avg_spend).sum())
    total = len(rfm_df)
    
    return {
        'avg_spend': round(float(avg_spend), 2) if not np.isnan(avg_spend) else 0,  # Lifetime value
        'avg_spend_per_visit': round(float(avg_spend_per_visit), 2) if not np.isnan(avg_spend_per_visit) else 0,
        'total_visits': int(total_visits),
        'median_spend': round(float(median_spend), 2) if not np.isnan(median_spend) else 0,
        'below_avg_count': below_avg,
        'below_avg_percentage': round(float((below_avg / total * 100) if total > 0 else 0), 2),
        'above_avg_count': above_avg,
        'above_avg_percentage': round(float((above_avg / total * 100) if total > 0 else 0), 2)
    }

def calculate_best_customers_by_shop(rfm_df, top_n=10):
    """Get best customers from each shop"""
    best_customers = {}
    
    # ✅ Use 'Shop' instead of 'Location'
    for shop_name in rfm_df['Shop'].unique():
        shop_customers = rfm_df[rfm_df['Shop'] == shop_name].copy()
        
        # Sort by RFM scores - MONETARY FIRST priority
        shop_customers = shop_customers.sort_values(
            by=['M_Score', 'Monetary', 'F_Score', 'R_Score'], 
            ascending=[False, False, False, False]
        )
        
        top_customers = shop_customers.head(top_n)[
            ['First_Name', 'Phone', 'Gender', 'RFM_Segment', 'Recency_Days', 'Frequency', 'Monetary', 'RFM_Combined']
        ].to_dict('records')
        
        # Make safe for JSON
        for customer in top_customers:
            customer['Recency_Days'] = int(customer['Recency_Days'])
            customer['Frequency'] = int(customer['Frequency'])
            customer['Monetary'] = round(float(customer['Monetary']), 2)
        
        best_customers[shop_name] = top_customers
    
    return best_customers

def calculate_best_customer_per_shop(rfm_df):
    """Get the single best customer from each shop"""
    best_per_shop = []
    
    for shop_name in rfm_df['Shop'].unique():
        shop_customers = rfm_df[rfm_df['Shop'] == shop_name].copy()
        
        # Sort by RFM scores to get the best customer - MONETARY FIRST priority
        shop_customers = shop_customers.sort_values(
            by=['M_Score', 'Monetary', 'F_Score', 'R_Score'], 
            ascending=[False, False, False, False]
        )
        
        if len(shop_customers) > 0:
            best = shop_customers.iloc[0]
            best_per_shop.append({
                'shop': shop_name,
                'first_name': best['First_Name'],
                'phone': best['Phone'],
                'gender': best['Gender'],
                'rfm_segment': best['RFM_Segment'],
                'recency_days': int(best['Recency_Days']),
                'frequency': int(best['Frequency']),
                'monetary': round(float(best['Monetary']), 2),
                'rfm_combined': best['RFM_Combined']
            })
    
    return sorted(best_per_shop, key=lambda x: x['monetary'], reverse=True)

def calculate_monthly_rfm_trends(df):
    """Calculate RFM trends over months"""
    df_clean = df.copy()
    df_clean['YearMonth'] = df_clean['Date'].dt.to_period('M')
    
    months = sorted(df_clean['YearMonth'].unique())
    monthly_trends = []
    
    for month in months:
        month_end = month.to_timestamp(how='end')
        month_data = df_clean[df_clean['YearMonth'] <= month]
        
        if len(month_data) == 0:
            continue
        
        # Use monthly period type for trends since we're looking at month-by-month
        rfm_month = calculate_rfm_scores(month_data, analysis_date=month_end, period_type='monthly')
        segment_dist = rfm_month['RFM_Segment'].value_counts().to_dict()
        
        monthly_trends.append({
            'month': str(month),
            'segments': {seg: int(count) for seg, count in segment_dist.items()},
            'total_customers': int(len(rfm_month))
        })
    
    return monthly_trends

def get_filtered_df(df, shop=None, period=None):
    """Filter dataframe by shop and time period"""
    temp_df = df.copy()
    records_before = len(temp_df)
    
    # 1. Apply Shop Filter - ✅ Using 'Shop' column
    if shop and shop != "All Shops":
        temp_df = temp_df[temp_df['Shop'] == shop]
        print(f"[DEBUG] After shop filter ({shop}): {len(temp_df)} records (removed {records_before - len(temp_df)})")
    
    # 2. Apply Period Filter
    if period and period != "Overall":
        records_before_period = len(temp_df)
        if "Quarter" in period or period.startswith("Q"):  # Example: "Q1 2025"
            q, year = period.split(' ')
            month_map = {"Q1": (1, 3), "Q2": (4, 6), "Q3": (7, 9), "Q4": (10, 12)}
            start_m, end_m = month_map[q]
            temp_df = temp_df[(temp_df['Date'].dt.year == int(year)) & 
                               (temp_df['Date'].dt.month >= start_m) & 
                               (temp_df['Date'].dt.month <= end_m)]
        elif "H1" in period or "H2" in period:  # Example: "H1 2025"
            h, year = period.split(' ')
            start_m, end_m = (1, 6) if h == "H1" else (7, 12)
            temp_df = temp_df[(temp_df['Date'].dt.year == int(year)) & 
                               (temp_df['Date'].dt.month >= start_m) & 
                               (temp_df['Date'].dt.month <= end_m)]
        elif len(period) == 4:  # Yearly: "2025"
            temp_df = temp_df[temp_df['Date'].dt.year == int(period)]
        else:  # Monthly: "January 2025"
            try:
                target_date = pd.to_datetime(period)
                print(f"[DEBUG] Filtering for period: {period} (parsed as {target_date.strftime('%B %Y')})")
                print(f"[DEBUG] Looking for year={target_date.year}, month={target_date.month}")
                print(f"[DEBUG] Date range in full data: {df['Date'].min()} to {df['Date'].max()}")
                print(f"[DEBUG] Records with month={target_date.month}: {(df['Date'].dt.month == target_date.month).sum()}")
                print(f"[DEBUG] Records with year={target_date.year}: {(df['Date'].dt.year == target_date.year).sum()}")
                print(f"[DEBUG] Records matching both year AND month: {((df['Date'].dt.year == target_date.year) & (df['Date'].dt.month == target_date.month)).sum()}")
                
                temp_df = temp_df[(temp_df['Date'].dt.year == target_date.year) & 
                                   (temp_df['Date'].dt.month == target_date.month)]
                print(f"[DEBUG] After period filter ({period}): {len(temp_df)} records (removed {records_before_period - len(temp_df)})")
            except Exception as e:
                print(f"[ERROR] Period parsing failed: {str(e)}")
                pass
    
    return temp_df


def calculate_customer_of_month(df, month_str):
    """Find customers qualifying for Customer of the Month award per shop."""
    try:
        target_date = pd.to_datetime(month_str)
        month_df = df[
            (df['Date'].dt.year == target_date.year) &
            (df['Date'].dt.month == target_date.month) &
            (df['Price'] > 0)
        ].copy()
    except Exception as e:
        print(f"[ERROR] Customer of Month filter failed: {str(e)}")
        return []

    if month_df.empty:
        return []

    agg = month_df.groupby(['Customer_ID', 'Shop']).agg(
        First_Name=('First Name', 'first'),
        Phone=('Phone', 'first'),
        Gender=('Gender', 'first'),
        Monthly_Spend=('Price', 'sum'),
        Visits=('Date', lambda x: x.dt.date.nunique())
    ).reset_index()

    results = []

    for shop in sorted(agg['Shop'].unique()):
        criteria = next(
            (AWARDING_CRITERIA[k] for k in AWARDING_CRITERIA if k.lower() == shop.lower()),
            None
        )

        shop_customers = agg[agg['Shop'] == shop].sort_values('Monthly_Spend', ascending=False)
        if shop_customers.empty:
            continue

        shop_status = criteria['status'] if criteria else 'no_criteria'
        min_spend = criteria['min_spend'] if criteria else None
        award_amount = criteria['award'] if criteria else None

        # Only the single top spender per shop qualifies for the award
        row = shop_customers.iloc[0]
        spend = float(row['Monthly_Spend'])

        if shop_status == 'active' and min_spend:
            qualifies = spend >= min_spend
            cust_status = 'qualified' if qualifies else 'not_qualified'
            cust_award = award_amount if qualifies else None
        elif shop_status == 'verify':
            qualifies = False
            cust_status = 'verify'
            cust_award = None
        elif shop_status == 'monitoring':
            qualifies = False
            cust_status = 'monitoring'
            cust_award = None
        else:
            qualifies = False
            cust_status = 'no_criteria'
            cust_award = None

        results.append({
            'shop': shop,
            'shop_status': shop_status,
            'min_spend': min_spend,
            'award_amount': award_amount,
            'customers': [{
                'first_name': str(row['First_Name']),
                'phone': str(row['Phone']),
                'gender': str(row['Gender']),
                'monthly_spend': round(spend, 2),
                'visits': int(row['Visits']),
                'qualifies': qualifies,
                'award_amount': cust_award,
                'status': cust_status
            }],
            'qualifying_count': 1 if qualifies else 0
        })

    return results


@app.route('/')
def index():
    """Render the RFM dashboard"""
    print("--- RFM Dashboard Home Accessed ---")
    return render_template('rfm_dashboard.html')

@app.route('/api/rfm-data')
def get_rfm_data():
    """API endpoint to get filtered RFM analysis data"""
    try:
        start_time = time.time()
        
        # 1. Get query parameters from the URL
        shop = request.args.get('shop', 'All Shops')
        period = request.args.get('period', 'Overall')
        
        # 2. Load the full dataset
        raw_df = get_customer_data()
        
        # 3. Generate filter options for the dropdowns based on raw data
        # ✅ Using 'Shop' column
        available_shops = sorted(raw_df['Shop'].unique().tolist())
        months = sorted(raw_df['Date'].dt.strftime('%B %Y').unique().tolist(), 
                        key=lambda x: pd.to_datetime(x))
        years = sorted(raw_df['Date'].dt.year.unique().astype(str).tolist())
        
        # Generate quarters
        quarters = []
        for year in years:
            for q in ['Q1', 'Q2', 'Q3', 'Q4']:
                quarters.append(f"{q} {year}")
        
        # Determine period type and analysis date
        period_type = get_period_type(period)
        analysis_date = get_analysis_date(period, raw_df)
        print(f"[INFO] Period: {period} -> Type: {period_type}")
        print(f"[INFO] Analysis Date: {analysis_date}")
        
        # 4. Filter the raw data based on shop and period FIRST
        # This gives us the transactions for the selected period/location
        filtered_transactions = get_filtered_df(raw_df, shop, period)
        
        if filtered_transactions.empty:
            return jsonify({'error': 'No data found for the selected filters'}), 404
        
        print(f"[DEBUG] Filtered transactions: {len(filtered_transactions)} records (includes combos with 0 price)")
        print(f"[DEBUG] Filtered transactions unique customers: {filtered_transactions['Customer_ID'].nunique()}")
        
        # Show sample Customer_IDs to check for issues
        print(f"[DEBUG] Sample Customer_IDs: {filtered_transactions['Customer_ID'].head(20).tolist()}")
        print(f"[DEBUG] Duplicate Customer_IDs: {len(filtered_transactions) - filtered_transactions['Customer_ID'].nunique()}")
        
        # Check for any records with null/empty Customer_ID
        empty_customer_ids = (filtered_transactions['Customer_ID'].isna() | (filtered_transactions['Customer_ID'] == '')).sum()
        print(f"[DEBUG] Records with null/empty Customer_ID: {empty_customer_ids}")
        
        print(f"[DEBUG] Records with null/zero Price in filtered transactions: {(filtered_transactions['Price'] <= 0).sum() + filtered_transactions['Price'].isna().sum()}")
        print(f"[DEBUG] Total spend in filtered data: {filtered_transactions['Price'].sum()}")
        print(f"[DEBUG] Avg price per transaction in filtered data: {filtered_transactions['Price'].mean()}")
        
        # 5. Calculate RFM scores on the FILTERED data (not full dataset)
        # This ensures F, M, R reflect only the selected period/location
        rfm_filtered = calculate_rfm_scores(filtered_transactions, analysis_date, period_type=period_type)
        
        # Debug output
        print(f"[DEBUG] RFM filtered customers: {len(rfm_filtered)}")
        print(f"[DEBUG] RFM average spend: {rfm_filtered['Monetary'].mean()}")
        print(f"[DEBUG] RFM total monetary: {rfm_filtered['Monetary'].sum()}")
        print(f"[DEBUG] Columns in rfm_filtered: {rfm_filtered.columns.tolist()}")
        print(f"[DEBUG] RFM Score sample: R={rfm_filtered['R_Score'].describe()}")
        print(f"[DEBUG] Filtered to {len(rfm_filtered)} customers for shop={shop}, period={period}")
        
        # Calculate date range info
        min_date = filtered_transactions['Date'].min()
        max_date = filtered_transactions['Date'].max()
        num_days = (max_date - min_date).days + 1  # +1 to include both start and end dates
        
        # 6. Calculate all metrics on the filtered data
        data = {
            'analysis_date': analysis_date.strftime('%Y-%m-%d'),
            'period_type': period_type,
            'date_range': {
                'start_date': min_date.strftime('%B %d, %Y'),
                'end_date': max_date.strftime('%B %d, %Y'),
                'num_days': int(num_days),
                'num_transactions': int(len(filtered_transactions))
            },
            'total_customers': int(len(rfm_filtered)),
            'segment_distribution': calculate_segment_distribution(rfm_filtered),
            'purchase_frequency_distribution': calculate_purchase_frequency_distribution(rfm_filtered),
            'shop_gender_distribution': calculate_shop_gender_distribution(rfm_filtered),
            'spending_analysis': calculate_spending_analysis(filtered_transactions, rfm_filtered),
            'best_customer_per_shop': calculate_best_customer_per_shop(rfm_filtered),
            'best_customers_by_shop': calculate_best_customers_by_shop(rfm_filtered),
            'monthly_trends': calculate_monthly_rfm_trends(filtered_transactions),
            'filters': {
                'shops': available_shops,
                'periods': {
                    'Monthly': months,
                    'Quarterly': quarters,
                    'Yearly': years,
                    'Semi_Annually': [f"H1 {y}" for y in years] + [f"H2 {y}" for y in years]
                }
            }
        }
        
        print(f"[TIME] Filtered calculation ({shop}/{period}) in {time.time() - start_time:.2f}s")
        return jsonify(data)
    
    except Exception as e:
        print(f"Error in /api/rfm-data: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/api/customer-of-month')
def get_customer_of_month():
    """API endpoint for Customer of the Month awarding list"""
    try:
        month = request.args.get('month', datetime.now().strftime('%B %Y'))
        raw_df = get_customer_data()
        results = calculate_customer_of_month(raw_df, month)

        total_qualifying = sum(r['qualifying_count'] for r in results)
        total_award_value = sum(
            c['award_amount'] for r in results
            for c in r['customers']
            if c['qualifies'] and c['award_amount']
        )

        return jsonify({
            'month': month,
            'total_qualifying': int(total_qualifying),
            'total_award_value': float(total_award_value),
            'results': results
        })
    except Exception as e:
        print(f"Error in /api/customer-of-month: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/refresh-now', methods=['POST'])
def refresh_now():
    """Force refresh data from Google Sheets"""
    global cached_data, last_fetch_time
    cached_data = None
    last_fetch_time = None
    return get_rfm_data()

if __name__ == '__main__':
    print("\n" + "="*60)
    print("Starting RFM Analysis Dashboard")
    print("="*60)
    print(f"[LINK] Open in browser: http://localhost:5001")
    print(f"[INFO] RFM Analysis using Customer Database")
    print("="*60 + "\n")
    
    app.run(debug=True, port=5001, host='0.0.0.0', use_reloader=False)
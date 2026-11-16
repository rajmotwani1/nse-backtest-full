import io, json, gzip, time, math, requests
import pandas as pd
import streamlit as st
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

st.set_page_config(page_title="BLUE WHALE PRO | NSE", page_icon="🐋", layout="wide")
IST = ZoneInfo("Asia/Kolkata")
BASE = "https://api.upstox.com"
NIFTY500_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv"
UPSTOX_NSE_INSTRUMENTS = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"

# ---------- Secrets / login ----------
def secret(name, default=""):
    try:
        return st.secrets.get(name, default)
    except Exception:
        return default

app_password = secret("APP_PASSWORD", "")
if app_password and not st.session_state.get("authenticated", False):
    st.title("🐋 BLUE WHALE PRO – NSE")
    pwd = st.text_input("Enter password", type="password")
    if st.button("Login", type="primary"):
        if pwd == app_password:
            st.session_state.authenticated = True
            st.rerun()
        st.error("Incorrect password")
    st.stop()

token = secret("UPSTOX_ACCESS_TOKEN", "")
if not token:
    st.title("🐋 BLUE WHALE PRO – NSE")
    st.error("Upstox Analytics Token missing. Add UPSTOX_ACCESS_TOKEN in Streamlit Settings → Secrets.")
    st.code('APP_PASSWORD = "your-private-password"\nUPSTOX_ACCESS_TOKEN = "paste-your-token-here"')
    st.stop()

HEADERS = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

@st.cache_data(ttl=3600, show_spinner=False)
def get_nifty500():
    headers = {"User-Agent":"Mozilla/5.0 Chrome/125.0 Safari/537.36", "Accept":"text/csv,*/*", "Referer":"https://www.nseindia.com/"}
    r = requests.get(NIFTY500_URL, headers=headers, timeout=30)
    r.raise_for_status()
    df = pd.read_csv(io.BytesIO(r.content)); df.columns = [str(c).strip() for c in df.columns]
    col = next((c for c in df.columns if c.lower() in ("symbol", "symbols")), None)
    if not col: raise ValueError("Nifty 500 CSV mein Symbol column nahi mila.")
    return sorted(df[col].dropna().astype(str).str.strip().unique().tolist())

@st.cache_data(ttl=3600, show_spinner=False)
def get_instrument_map():
    r = requests.get(UPSTOX_NSE_INSTRUMENTS, timeout=40); r.raise_for_status()
    raw = gzip.decompress(r.content); items = json.loads(raw.decode("utf-8")); out = {}
    for x in items:
        if x.get("segment") == "NSE_EQ" and x.get("instrument_type") == "EQ":
            out[str(x.get("trading_symbol", "")).upper()] = x.get("instrument_key")
    return out

def upstox_get(path, params=None):
    r = requests.get(BASE + path, headers=HEADERS, params=params, timeout=30)
    if r.status_code == 401: raise RuntimeError("Upstox token invalid/expired. Analytics Token refresh karo.")
    if r.status_code == 429: raise RuntimeError("Upstox rate limit hit. Batch chhota karo aur kuch der baad retry karo.")
    r.raise_for_status(); payload = r.json()
    if payload.get("status") not in (None, "success"): raise RuntimeError(str(payload))
    return payload.get("data", {})

@st.cache_data(ttl=20, show_spinner=False)
def get_quotes(keys_tuple, token_fingerprint):
    data = upstox_get("/v3/market-quote/quotes", {"instrument_key": ",".join(keys_tuple)})
    rows=[]
    for key,q in data.items():
        rows.append({"Instrument":str(key),"Quote Symbol":q.get("symbol") or q.get("trading_symbol") or q.get("short_name"),"LTP":q.get("last_price"),"Change":q.get("net_change"),"Volume":q.get("volume"),"Last trade time":q.get("last_trade_time")})
    return pd.DataFrame(rows)

def parse_candle_rows(rows):
    if not rows: return pd.DataFrame(columns=["ts","open","high","low","close","volume","oi"])
    df = pd.DataFrame(rows, columns=["ts","open","high","low","close","volume","oi"])
    df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert("Asia/Kolkata")
    for c in ["open","high","low","close","volume"]: df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["ts","open","high","low","close"]).sort_values("ts").drop_duplicates("ts",keep="last").reset_index(drop=True)

@st.cache_data(ttl=86400, show_spinner=False)
def fetch_candle_chunk(instrument_key, unit, interval, to_date, from_date, token_fingerprint):
    path = f"/v3/historical-candle/{requests.utils.quote(instrument_key, safe='')}/{unit}/{interval}/{to_date}/{from_date}"
    return parse_candle_rows(upstox_get(path).get("candles", []))

def candles_range(instrument_key, unit, interval, start_date, end_date):
    """Fetch full selected date range in API-safe chunks and combine without duplicate timestamps."""
    if start_date > end_date: return pd.DataFrame(columns=["ts","open","high","low","close","volume","oi"])
    # V3: minute intervals >15 and hourly intervals are limited to a quarter per request.
    if unit == "minutes": chunk_days = 27 if int(interval) <= 15 else 75
    elif unit == "hours": chunk_days = 75
    else: chunk_days = 3500
    frames=[]; cursor=start_date
    while cursor <= end_date:
        chunk_end=min(end_date, cursor+timedelta(days=chunk_days))
        frames.append(fetch_candle_chunk(instrument_key,unit,interval,chunk_end.isoformat(),cursor.isoformat(),token[-6:]))
        cursor=chunk_end+timedelta(days=1)
        if len(frames) > 100: raise RuntimeError("Date range bahut bada hai; smaller period ya smaller batch select karo.")
    if not frames: return pd.DataFrame(columns=["ts","open","high","low","close","volume","oi"])
    return pd.concat(frames,ignore_index=True).sort_values("ts").drop_duplicates("ts",keep="last").reset_index(drop=True)

def candles_recent(instrument_key, unit, interval, days_back):
    end=date.today(); start=end-timedelta(days=int(days_back))
    return candles_range(instrument_key,unit,interval,start,end)

def intraday_candles(instrument_key, unit, interval):
    path=f"/v3/historical-candle/intraday/{requests.utils.quote(instrument_key,safe='')}/{unit}/{interval}"
    return parse_candle_rows(upstox_get(path).get("candles",[]))

def merge_candle_sets(a,b):
    if a.empty:return b.copy()
    if b.empty:return a.copy()
    return pd.concat([a,b],ignore_index=True).sort_values("ts").drop_duplicates("ts",keep="last").reset_index(drop=True)

def is_nse_regular_session(now=None):
    now=now or datetime.now(IST)
    return now.weekday()<5 and (now.hour,now.minute)>=(9,15) and (now.hour,now.minute)<(15,30)

def map_poc_to_base(htf, base):
    """Original Pine formula: (previous HTF high + previous HTF low + previous HTF close + current HTF open)/4."""
    if len(htf)<2 or base.empty:return pd.Series(index=base.index,dtype="float64")
    h=htf[["ts","open","high","low","close"]].copy().sort_values("ts")
    h["poc"]=(h["high"].shift(1)+h["low"].shift(1)+h["close"].shift(1)+h["open"])/4.0
    h=h[["ts","poc"]].dropna().sort_values("ts")
    aligned=pd.merge_asof(base[["ts"]].sort_values("ts"),h,on="ts",direction="backward")
    return pd.Series(aligned["poc"].to_numpy(),index=base.index,dtype="float64")

def make_condition(close,poc,direction,win_bars=3):
    crosses=[]
    for i in range(len(close)):
        if i==0 or pd.isna(poc.iloc[i]) or pd.isna(poc.iloc[i-1]): crosses.append(False); continue
        if direction=="BUY": crosses.append(close.iloc[i]>poc.iloc[i] and close.iloc[i-1]<=poc.iloc[i-1])
        else: crosses.append(close.iloc[i]<poc.iloc[i] and close.iloc[i-1]>=poc.iloc[i-1])
    flags=[]; last_cross=None
    for i,crossed in enumerate(crosses):
        if crossed:last_cross=i
        within=last_cross is not None and (i-last_cross)<=win_bars
        flags.append(within and (close.iloc[i]>poc.iloc[i] if direction=="BUY" else close.iloc[i]<poc.iloc[i]))
    return pd.Series(flags,index=close.index,dtype=bool)

def signal_frame(base,h4,daily,weekly,atr_period=14):
    """Generate original Blue Whale new-signal events over every candle in the requested range."""
    if min(len(base),len(h4),len(daily),len(weekly))<4:return pd.DataFrame()
    base=base.sort_values("ts").reset_index(drop=True).copy()
    p4=map_poc_to_base(h4,base); pd_=map_poc_to_base(daily,base); pw=map_poc_to_base(weekly,base)
    valid=p4.notna() & pd_.notna() & pw.notna()
    base=base.loc[valid].reset_index(drop=True); p4=p4.loc[valid].reset_index(drop=True); pd_=pd_.loc[valid].reset_index(drop=True); pw=pw.loc[valid].reset_index(drop=True)
    if len(base)<4:return pd.DataFrame()
    close=base["close"].astype(float)
    up4=make_condition(close,p4,"BUY",3); upd=make_condition(close,pd_,"BUY",3); upw=make_condition(close,pw,"BUY",3)
    dn4=make_condition(close,p4,"SELL",3); dnd=make_condition(close,pd_,"SELL",3); dnw=make_condition(close,pw,"SELL",3)
    up_cond=up4 & upd & upw & (close>p4) & (close>pd_) & (close>pw)
    dn_cond=dn4 & dnd & dnw & (close<p4) & (close<pd_) & (close<pw)
    buy_events=up_cond & ~up_cond.shift(1,fill_value=False)
    sell_events=dn_cond & ~dn_cond.shift(1,fill_value=False)
    out=base.copy(); out["Signal"]=""; out.loc[buy_events,"Signal"]="BUY"; out.loc[sell_events,"Signal"]="SELL"
    out["4H POC"]=p4.to_numpy(); out["Daily POC"]=pd_.to_numpy(); out["Weekly POC"]=pw.to_numpy()
    out["ATR14"]=atr_series(out,atr_period)
    return out[out["Signal"]!=""].reset_index(drop=True)

def atr_series(df,period=14):
    prev=df["close"].shift(1)
    tr=pd.concat([(df["high"]-df["low"]),(df["high"]-prev).abs(),(df["low"]-prev).abs()],axis=1).max(axis=1)
    return tr.rolling(period,min_periods=period).mean()

def calculate_levels(side,entry,signal_high,signal_low,atr,sl_method,sl_value,target_method,target_value,rr_value,candle_buffer):
    """SL and target methods are independently selectable. Returns (SL, target)."""
    if side=="BUY":
        if sl_method=="Fixed Points": sl=entry-float(sl_value)
        elif sl_method=="Percentage": sl=entry*(1-float(sl_value)/100)
        elif sl_method=="Signal Candle High/Low": sl=float(signal_low)-float(candle_buffer)
        elif sl_method=="ATR Based": sl=entry-(float(atr)*float(sl_value))
        else: sl=entry-float(sl_value)  # Risk:Reward uses configurable risk points as its base stop
        risk=max(entry-sl,0.000001)
        if target_method=="Fixed Points": target=entry+float(target_value)
        elif target_method=="Percentage": target=entry*(1+float(target_value)/100)
        elif target_method=="Signal Candle High/Low": target=float(signal_high)+max(float(signal_high)-float(signal_low),0)
        elif target_method=="ATR Based": target=entry+(float(atr)*float(target_value))
        else: target=entry+risk*float(rr_value)
    else:
        if sl_method=="Fixed Points": sl=entry+float(sl_value)
        elif sl_method=="Percentage": sl=entry*(1+float(sl_value)/100)
        elif sl_method=="Signal Candle High/Low": sl=float(signal_high)+float(candle_buffer)
        elif sl_method=="ATR Based": sl=entry+(float(atr)*float(sl_value))
        else: sl=entry+float(sl_value)
        risk=max(sl-entry,0.000001)
        if target_method=="Fixed Points": target=entry-float(target_value)
        elif target_method=="Percentage": target=entry*(1-float(target_value)/100)
        elif target_method=="Signal Candle High/Low": target=float(signal_low)-max(float(signal_high)-float(signal_low),0)
        elif target_method=="ATR Based": target=entry-(float(atr)*float(target_value))
        else: target=entry-risk*float(rr_value)
    return round(float(sl),4),round(float(target),4)

def simulate_trades(events,all_candles,sl_method,sl_value,target_method,target_value,rr_value,candle_buffer,qty,fee_per_side):
    if events.empty or all_candles.empty:return []
    bars=all_candles.sort_values("ts").reset_index(drop=True)
    idx_by_ts={ts:i for i,ts in enumerate(bars["ts"])}
    trades=[]; next_free=0
    for _,sig in events.iterrows():
        si=idx_by_ts.get(sig["ts"])
        if si is None or si+1>=len(bars) or si<next_free:continue
        entry_bar=bars.iloc[si+1]; entry=float(entry_bar["open"]); side=sig["Signal"]
        atr=float(sig["ATR14"]) if pd.notna(sig["ATR14"]) and float(sig["ATR14"])>0 else float(sig["high"]-sig["low"])
        if not math.isfinite(atr) or atr<=0:continue
        sl,tp=calculate_levels(side,entry,float(sig["high"]),float(sig["low"]),atr,sl_method,sl_value,target_method,target_value,rr_value,candle_buffer)
        if (side=="BUY" and (sl>=entry or tp<=entry)) or (side=="SELL" and (sl<=entry or tp>=entry)):continue
        exit_price=None; exit_reason="End of data"; exit_idx=len(bars)-1
        for j in range(si+1,len(bars)):
            b=bars.iloc[j]; o=float(b["open"]); hi=float(b["high"]); lo=float(b["low"])
            if side=="BUY":
                if o<=sl: exit_price=o; exit_reason="Gap SL"; exit_idx=j; break
                if o>=tp: exit_price=o; exit_reason="Gap Target"; exit_idx=j; break
                hit_sl=lo<=sl; hit_tp=hi>=tp
            else:
                if o>=sl: exit_price=o; exit_reason="Gap SL"; exit_idx=j; break
                if o<=tp: exit_price=o; exit_reason="Gap Target"; exit_idx=j; break
                hit_sl=hi>=sl; hit_tp=lo<=tp
            # Conservative intrabar assumption: if both touched in one candle, count SL first.
            if hit_sl: exit_price=sl; exit_reason="Stop Loss"; exit_idx=j; break
            if hit_tp: exit_price=tp; exit_reason="Target"; exit_idx=j; break
        if exit_price is None:exit_price=float(bars.iloc[-1]["close"])
        gross=(exit_price-entry)*qty if side=="BUY" else (entry-exit_price)*qty
        net=gross-2*float(fee_per_side)*qty
        trades.append({"Stock":sig.get("Stock",""),"Timeframe":sig.get("Timeframe",""),"SL Method":sl_method,"SL Setting":sl_value,"Target Method":target_method,"Target Setting":target_value,"RR Multiplier":rr_value,"Signal":side,"Signal Time":str(sig["ts"]),"Entry Time":str(entry_bar["ts"]),"Entry":round(entry,2),"Stop Loss":round(sl,2),"Target":round(tp,2),"Exit Time":str(bars.iloc[exit_idx]["ts"]),"Exit":round(float(exit_price),2),"Exit Reason":exit_reason,"Qty":qty,"P&L":round(net,2),"P&L Points":round((exit_price-entry) if side=="BUY" else (entry-exit_price),2)})
        next_free=exit_idx
    return trades

def backtest_one(symbol,key,tf,start_date,end_date,settings):
    units={"30m":("minutes","30"),"1H":("hours","1"),"2H":("hours","2"),"3H":("hours","3"),"4H":("hours","4")}
    unit,interval=units[tf]
    # Warm-up data before selected start is needed for POC and ATR calculations.
    warm_start=start_date-timedelta(days=35)
    base=candles_range(key,unit,interval,warm_start,end_date)
    h4=candles_range(key,"hours","4",warm_start,end_date)
    daily=candles_range(key,"days","1",start_date-timedelta(days=400),end_date)
    weekly=candles_range(key,"weeks","1",start_date-timedelta(days=900),end_date)
    if min(len(base),len(h4),len(daily),len(weekly))<20:return []
    events=signal_frame(base,h4,daily,weekly,settings.get("atr_period",14))
    if events.empty:return []
    events=events[(events["ts"].dt.date>=start_date)&(events["ts"].dt.date<=end_date)].copy()
    events["Stock"]=symbol; events["Timeframe"]=tf
    return simulate_trades(events,base,sl_method=settings["sl_method"],sl_value=settings["sl_value"],target_method=settings["target_method"],target_value=settings["target_value"],rr_value=settings["rr_value"],candle_buffer=settings["candle_buffer"],qty=settings["qty"],fee_per_side=settings["fee_per_side"])

def scan_one(symbol,key,tf,settings):
    unit,interval={"30m":("minutes","30"),"1H":("hours","1"),"2H":("hours","2"),"3H":("hours","3"),"4H":("hours","4")}[tf]
    end=date.today(); start=end-timedelta(days=100)
    base=candles_range(key,unit,interval,start,end)
    h4=candles_range(key,"hours","4",start,end)
    daily=candles_range(key,"days","1",end-timedelta(days=450),end)
    weekly=candles_range(key,"weeks","1",end-timedelta(days=950),end)
    if is_nse_regular_session():
        try:base=merge_candle_sets(base,intraday_candles(key,unit,interval))
        except Exception:pass
    events=signal_frame(base,h4,daily,weekly,settings.get("atr_period",14))
    cutoff=pd.Timestamp(datetime.now(IST)-timedelta(days=30))
    events=events[events["ts"]>=cutoff]
    if events.empty:return None
    sig=events.iloc[-1]
    next_row=base[base["ts"]>sig["ts"]]
    entry=float(next_row.iloc[0]["open"]) if not next_row.empty else float(sig["close"])
    atr=float(sig["ATR14"]) if pd.notna(sig["ATR14"]) and sig["ATR14"]>0 else float(sig["high"]-sig["low"])
    sl,tp=calculate_levels(sig["Signal"],entry,float(sig["high"]),float(sig["low"]),atr,settings["sl_method"],settings["sl_value"],settings["target_method"],settings["target_value"],settings["rr_value"],settings["candle_buffer"])
    return {"Stock":symbol,"Signal":sig["Signal"],"Timeframe":tf,"Entry":round(entry,2),"Stop Loss":round(sl,2),"Target":round(tp,2),"4H POC":round(float(sig["4H POC"]),2),"Daily POC":round(float(sig["Daily POC"]),2),"Weekly POC":round(float(sig["Weekly POC"]),2),"Signal Candle High":round(float(sig["high"]),2),"Signal Candle Low":round(float(sig["low"]),2),"Candle time":str(sig["ts"])}


def render_yearwise_analysis(trades_df):
    """Calendar-year summary based on trade entry year, with per-stock annual rankings."""
    if trades_df is None or trades_df.empty or "Entry Time" not in trades_df.columns:
        st.info("Year-wise report ke liye completed trades available nahi hain.")
        return
    yearly = trades_df.copy()
    yearly["Entry Timestamp"] = pd.to_datetime(yearly["Entry Time"], errors="coerce")
    yearly = yearly.dropna(subset=["Entry Timestamp"]).copy()
    if yearly.empty:
        st.warning("Trade entry timestamps parse nahi ho paaye; year-wise report generate nahi hui.")
        return
    yearly["Year"] = yearly["Entry Timestamp"].dt.year.astype(int)
    yearly["Win"] = yearly["P&L"] > 0

    def summarize(group):
        wins = int(group["Win"].sum())
        losses = int((~group["Win"]).sum())
        gp = float(group.loc[group["P&L"] > 0, "P&L"].sum())
        gl = abs(float(group.loc[group["P&L"] < 0, "P&L"].sum()))
        return pd.Series({
            "Trades": len(group),
            "Wins": wins,
            "Losses": losses,
            "Win Rate %": round(100 * wins / len(group), 1) if len(group) else 0.0,
            "Net P&L": round(float(group["P&L"].sum()), 2),
            "Avg P&L / Trade": round(float(group["P&L"].mean()), 2),
            "Profit Factor": round(gp / gl, 2) if gl else (float("inf") if gp else 0.0),
        })

    annual = yearly.groupby("Year", sort=True).apply(summarize, include_groups=False).reset_index()
    st.subheader("Year-wise overall performance")
    st.caption("Trade ko us calendar year mein count kiya gaya hai jis year mein entry hui thi.")
    st.dataframe(annual, use_container_width=True, hide_index=True)
    chart_data = annual.set_index("Year")[["Net P&L"]]
    st.bar_chart(chart_data)

    stock_year = yearly.groupby(["Year", "Stock"], sort=True).apply(summarize, include_groups=False).reset_index()
    st.subheader("Year-wise stock performance")
    years = sorted(stock_year["Year"].unique().tolist())
    chosen_year = st.selectbox("Select year to see best-performing stocks", years, index=len(years)-1, key="yearwise_stock_year")
    selected = stock_year[stock_year["Year"] == chosen_year].copy()
    selected = selected.sort_values(["Net P&L", "Win Rate %", "Trades"], ascending=[False, False, False])
    st.dataframe(selected, use_container_width=True, hide_index=True)

    st.subheader("All years – stock comparison")
    st.dataframe(stock_year.sort_values(["Year", "Net P&L"], ascending=[True, False]), use_container_width=True, hide_index=True)
    st.download_button(
        "Download year-wise stock report CSV",
        stock_year.to_csv(index=False).encode("utf-8"),
        file_name="blue_whale_yearwise_stock_performance.csv",
        mime="text/csv",
        key="download_yearwise_stock_report",
    )
    st.download_button(
        "Download annual summary CSV",
        annual.to_csv(index=False).encode("utf-8"),
        file_name="blue_whale_annual_performance.csv",
        mime="text/csv",
        key="download_annual_summary",
    )

# ---------- Main UI ----------
st.title("🐋 BLUE WHALE PRO – NSE")
st.caption("Nifty 500 | Upstox Historical Data | Original Blue Whale POC entry logic preserved | Backtesting + configurable SL/Target")
now=datetime.now(IST)
if is_nse_regular_session(now):st.success("NSE regular session open. Scanner will use completed candles; backtest uses historical candles.")
else:st.info(f"NSE session closed. Current IST: {now.strftime('%d-%b-%Y %I:%M %p')}")

try:
    symbols=get_nifty500(); instrument_map=get_instrument_map(); universe=[(s,instrument_map[s]) for s in symbols if s in instrument_map]
except Exception as e:
    st.error(f"Stock list load nahi hui: {e}"); st.info("NSE list ya Upstox instrument master temporarily unavailable ho sakta hai."); st.stop()

c1,c2,c3=st.columns(3); c1.metric("Nifty 500 constituents",len(symbols)); c2.metric("Upstox matched stocks",len(universe)); c3.metric("Data mode","Read-only")

# Shared configurable exit rules
with st.sidebar:
    st.header("SL / Target Settings")
    methods=["Fixed Points","Percentage","Risk:Reward","Signal Candle High/Low","ATR Based"]
    sl_method=st.selectbox("Stop Loss method",methods,index=0)
    sl_value=st.number_input("SL value (points / % / ATR multiplier)",min_value=0.01,value=20.0,step=0.5)
    target_method=st.selectbox("Target method",methods,index=2)
    target_value=st.number_input("Target value (points / % / ATR multiplier)",min_value=0.01,value=40.0,step=0.5)
    rr_value=st.number_input("Risk : Reward multiplier",min_value=0.1,value=2.0,step=0.1)
    candle_buffer=st.number_input("Signal candle SL buffer (points)",min_value=0.0,value=0.0,step=0.05)
    atr_period=st.selectbox("ATR period",[7,10,14,20],index=2)
    qty=st.number_input("Quantity (shares)",min_value=1,value=1,step=1)
    fee=st.number_input("Estimated charges per side per share (₹)",min_value=0.0,value=0.0,step=0.01)
    st.caption("Risk:Reward SL uses SL value as risk points; target uses the RR multiplier. Signal-candle target uses range projection.")
settings={"sl_method":sl_method,"sl_value":sl_value,"target_method":target_method,"target_value":target_value,"rr_value":rr_value,"candle_buffer":candle_buffer,"qty":int(qty),"fee_per_side":fee,"atr_period":atr_period}


tab1,tab2,tab3,tab4=st.tabs(["Market Watch","Blue Whale Scanner","Backtesting Lab","Last Alerts"])
with tab1:
    st.subheader("Nifty 500 – Live Quote Snapshot")
    if st.button("Refresh all 500 quotes",type="primary"):
        try:
            qdf=get_quotes(tuple(k for _,k in universe[:500]),token[-6:])
            key_to_symbol={str(k).strip():s for s,k in universe}
            if not qdf.empty:
                qdf["Stock"]=qdf["Instrument"].map(key_to_symbol).fillna(qdf["Quote Symbol"])
                st.dataframe(qdf[["Stock","LTP","Change","Volume","Last trade time"]],use_container_width=True,hide_index=True)
            else:st.warning("Quote data empty. Market closed ho sakta hai.")
        except Exception as e:st.error(f"Quote API error: {e}")
    else:st.info("Snapshot dekhne ke liye Refresh all 500 quotes dabao.")

with tab2:
    st.subheader("Blue Whale Signal Scanner – Last 30 Days")
    tf=st.selectbox("Scanner timeframe",["30m","1H","2H","3H","4H"])
    batch_size=st.selectbox("Stocks per scan batch",[10,20,25,50],index=2)
    total_batches=max(1,math.ceil(len(universe)/batch_size))
    batch_no=st.number_input("Batch number",min_value=1,max_value=total_batches,value=1,step=1)
    batch=universe[(int(batch_no)-1)*batch_size:int(batch_no)*batch_size]
    st.caption(f"Batch {batch_no}/{total_batches}: {len(batch)} stocks. SL/Target settings sidebar se apply hongi.")
    if st.button("Run Blue Whale scan",type="primary"):
        found=[]; errors=[]; prog=st.progress(0); status=st.empty()
        for i,(sym,key) in enumerate(batch):
            status.write(f"Scanning {sym} ({i+1}/{len(batch)})...")
            try:
                sig=scan_one(sym,key,tf,settings)
                if sig:
                    found.append(sig); st.session_state.setdefault("alerts",[]); st.session_state.alerts.insert(0,sig)
            except Exception as e:errors.append(f"{sym}: {str(e)[:180]}")
            prog.progress((i+1)/max(len(batch),1)); time.sleep(0.12)
        st.session_state["last_scan"]=found; st.session_state["scan_errors"]=errors
        if found:st.success(f"{len(found)} stock(s) mein recent Blue Whale signal mila."); st.dataframe(pd.DataFrame(found),use_container_width=True,hide_index=True)
        else:st.info("Is batch mein past 30 days ka signal nahi mila.")
        if errors:
            with st.expander(f"{len(errors)} data/API issue(s)"):st.write("\n".join(errors[:30]))

with tab3:
    st.subheader("Historical Backtesting – January 2022 onwards")
    if st.button("Clear combined backtest results"):
        st.session_state.pop("bt_trades", None); st.session_state.pop("bt_errors", None); st.rerun()
    st.warning("Backtest Upstox API se selected stock batch aur timeframe ka data download karta hai. 500 stocks × multiple years × multiple timeframes bahut saari API calls hain; pehle 10–25 stocks se test karo. Data cache hota hai, lekin Streamlit restart par cache reset ho sakta hai.")
    b1,b2,b3=st.columns(3)
    with b1:bt_start=st.date_input("From date",value=date(2022,1,1),min_value=date(2022,1,1),max_value=date.today())
    with b2:bt_end=st.date_input("To date",value=date.today(),min_value=date(2022,1,1),max_value=date.today())
    with b3:bt_tf=st.selectbox("Backtest timeframe",["30m","1H","2H","3H","4H"],index=1)
    bt_batch_size=st.selectbox("Stocks per backtest batch",[5,10,25,50,100,500],index=2)
    bt_batches=max(1,math.ceil(len(universe)/bt_batch_size))
    bt_batch_no=st.number_input("Backtest batch number",min_value=1,max_value=bt_batches,value=1,step=1,key="bt_batch")
    bt_batch=universe[(int(bt_batch_no)-1)*bt_batch_size:int(bt_batch_no)*bt_batch_size]
    st.caption(f"Selected batch: {len(bt_batch)} stocks | {bt_tf} | {bt_start} to {bt_end}")
    if bt_start>bt_end:st.error("From date, To date se pehle honi chahiye.")
    run=st.button("Run Historical Backtest",type="primary",disabled=bt_start>bt_end)
    if run:
        all_trades=[]; errors=[]; progress=st.progress(0); status=st.empty()
        for i,(sym,key) in enumerate(bt_batch):
            status.write(f"Downloading & backtesting {sym} ({i+1}/{len(bt_batch)})...")
            try:all_trades.extend(backtest_one(sym,key,bt_tf,bt_start,bt_end,settings))
            except Exception as e:errors.append(f"{sym}: {str(e)[:220]}")
            progress.progress((i+1)/max(len(bt_batch),1)); time.sleep(0.10)
        new_tdf=pd.DataFrame(all_trades)
        previous=st.session_state.get("bt_trades",pd.DataFrame())
        if not previous.empty and not new_tdf.empty:
            tdf=pd.concat([previous,new_tdf],ignore_index=True)
            dedupe=[c for c in ["Stock","Timeframe","Signal Time","SL Method","SL Setting","Target Method","Target Setting","RR Multiplier"] if c in tdf.columns]
            tdf=tdf.drop_duplicates(subset=dedupe,keep="last").reset_index(drop=True)
        elif not new_tdf.empty:tdf=new_tdf
        else:tdf=previous
        st.session_state["bt_trades"]=tdf
        st.session_state["bt_errors"]=errors
        if tdf.empty:st.info("Selected batch/period mein completed trades nahi mile. API errors ya insufficient historical candles bhi check karo.")
        else:
            st.success(f"Combined results: {len(tdf)} trades. Different batches run karke same results mein add kar sakte ho.")
            wins=int((tdf["P&L"]>0).sum()); losses=int((tdf["P&L"]<=0).sum()); gross_profit=float(tdf.loc[tdf["P&L"]>0,"P&L"].sum()); gross_loss=abs(float(tdf.loc[tdf["P&L"]<0,"P&L"].sum()))
            winrate=100*wins/len(tdf); net=float(tdf["P&L"].sum()); eq=tdf["P&L"].cumsum(); dd=float((eq-eq.cummax()).min()) if len(eq) else 0
            m1,m2,m3,m4,m5=st.columns(5); m1.metric("Total Trades",len(tdf)); m2.metric("Win Rate",f"{winrate:.1f}%"); m3.metric("Winning / Losing",f"{wins} / {losses}"); m4.metric("Net P&L",f"₹{net:,.2f}"); m5.metric("Max Drawdown",f"₹{dd:,.2f}")
            st.metric("Profit Factor",f"{(gross_profit/gross_loss):.2f}" if gross_loss else ("∞" if gross_profit else "—"))
            by_stock=tdf.groupby("Stock").agg(Trades=("P&L","size"),Wins=("P&L",lambda x:int((x>0).sum())),Net_PnL=("P&L","sum"),Avg_PnL=("P&L","mean")).reset_index()
            by_stock["Win Rate %"]=(by_stock["Wins"]/by_stock["Trades"]*100).round(1)
            st.subheader("Stock-wise performance ranking")
            st.dataframe(by_stock.sort_values(["Net_PnL","Win Rate %"],ascending=False),use_container_width=True,hide_index=True)
            render_yearwise_analysis(tdf)
            st.subheader("Trade history")
            st.dataframe(tdf,use_container_width=True,hide_index=True)
            st.download_button("Download trades CSV",tdf.to_csv(index=False).encode("utf-8"),file_name="blue_whale_backtest_trades.csv",mime="text/csv")
            st.download_button("Download stock ranking CSV",by_stock.to_csv(index=False).encode("utf-8"),file_name="blue_whale_stock_ranking.csv",mime="text/csv")
        if errors:
            with st.expander(f"{len(errors)} API/data issue(s)"):st.write("\n".join(errors[:50]))
    elif "bt_trades" in st.session_state and not st.session_state["bt_trades"].empty:
        st.caption("Last completed backtest result")
        previous_tdf=st.session_state["bt_trades"]
        render_yearwise_analysis(previous_tdf)
        st.subheader("Combined trade history")
        st.dataframe(previous_tdf,use_container_width=True,hide_index=True)

with tab4:
    st.subheader("Last Blue Whale Alerts")
    alerts=st.session_state.get("alerts",[])
    if alerts:
        st.dataframe(pd.DataFrame(alerts[:300]),use_container_width=True,hide_index=True)
        if st.button("Clear alert list"):st.session_state["alerts"]=[]; st.rerun()
        st.caption("Alert history abhi current app session mein rahegi; permanent history ke liye database integration alag se add karna hoga.")
    else:st.info("Abhi alerts nahi hain. Scanner tab mein Run Blue Whale scan dabao.")

st.divider()
st.caption("Educational research/backtesting tool only. No orders are placed. Backtest assumes next-candle-open entry and conservatively counts SL first if SL and target are both touched in the same candle. Brokerage, taxes, slippage and liquidity may change real results.")

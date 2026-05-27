import streamlit as st
import pandas as pd
import numpy as np
from sqlalchemy import create_engine
import datetime
import folium
from streamlit_folium import st_folium
from folium.plugins import AntPath

# ==========================================
# 1. 初始化與資料庫連線
# ==========================================
st.set_page_config(page_title="YouBike 智慧調度系統", layout="wide")
st.title("🚲 YouBike 智慧調度決策與空間模擬系統")

# ⚠️ 請確保這是你正確的資料庫連線字串
DB_URI = st.secrets["DB_URI"]
@st.cache_resource
def init_connection():
    return create_engine(
        DB_URI,
        pool_size=20,
        max_overflow=20,
        pool_pre_ping=True,
        pool_recycle=3600
    )

engine = init_connection()

# 定義台北市 12 行政區
TAIPEI_DISTRICTS = ["全台北市 (預設)", "中正區", "大同區", "中山區", "松山區", "大安區", "萬華區", "信義區", "士林區", "北投區", "內湖區", "南港區", "文山區"]

# ==========================================
# 2. 側邊欄：雙模式設計與情境過濾器
# ==========================================
st.sidebar.header("🎛️ 分析與調度設定")

# --- A. 模式選擇 ---
analysis_mode = st.sidebar.radio(
    "選擇系統模式",
    ["📊 歷史趨勢模式 (看平均狀態)", "🔗 智慧調度模式 (生成搬運工單)"]
)
st.sidebar.markdown("---")

# --- B. 時間設定 (依模式動態變化) ---
st.sidebar.subheader("📅 時間設定")

if analysis_mode == "📊 歷史趨勢模式 (看平均狀態)":
    st.sidebar.caption("撈取多日資料算平均，不生成調度工單。")
    # 歷史趨勢：日期起訖 (選填)、時間 (選填)
    use_date_range = st.sidebar.checkbox("限定日期區間", value=False)
    if use_date_range:
        date_range = st.sidebar.date_input("選擇區間 (起-迄)", value=(datetime.date(2024, 11, 1), datetime.date(2024, 11, 15)))
        if len(date_range) == 2:
            start_date, end_date = date_range
        else:
            start_date = end_date = date_range[0]
            
    use_hour = st.sidebar.checkbox("限定特定時段", value=False)
    if use_hour:
        target_hour = st.sidebar.slider("選擇小時 (0-23)", 0, 23, 17)

elif analysis_mode == "🔗 智慧調度模式 (生成搬運工單)":
    st.sidebar.caption("為了算出準確的調度量，必須指定「小時」。")
    # 智慧調度：日期起訖 (選填)、時間 (必填)
    use_date_range = st.sidebar.checkbox("限定日期區間", value=False)
    if use_date_range:
        date_range = st.sidebar.date_input("選擇區間 (起-迄)", value=(datetime.date(2024, 11, 1), datetime.date(2024, 11, 15)))
        if len(date_range) == 2:
            start_date, end_date = date_range
        else:
            start_date = end_date = date_range[0]
            
    # 時間必填
    target_hour = st.sidebar.slider("⭐ 指定時段 (必填)", 0, 23, 17)


# --- C. 行政區選擇 (選填) ---
st.sidebar.markdown("---")
st.sidebar.subheader("📍 區域設定 (選填)")
selected_district = st.sidebar.selectbox("選擇行政區", TAIPEI_DISTRICTS)

# --- D. 氣候過濾 (選填) ---
st.sidebar.markdown("---")
st.sidebar.subheader("🌤️ 氣候情境 (選填)")
weather_temp = st.sidebar.selectbox("🌡️ 氣溫感受", ["無指定", "微涼 (< 20°C)", "舒適 (20-26°C)", "微熱 (26-28°C)", "炎熱 (28-33°C)", "過熱 (> 33°C)"])
weather_rain = st.sidebar.selectbox("☔ 降雨情況", ["無指定", "無雨 (0mm)", "毛毛雨 (0.1-5mm)", "大雨 (5.1-15mm)", "大暴雨 (> 15mm)"])

# ==========================================
# 3. 動態組裝 SQL 查詢 (雙模式共用底層，但分歧處理)
# ==========================================
# 為了能根據行政區過濾，若 `station_risk_status` 沒存 sarea，我們可以在 SQL 裡用語法模糊比對 sname (通常站名會包含區名，或者我們暫時用 sname 來做粗略過濾，若你們資料庫有 sarea 欄位請自行替換)。
# 這裡先假設你們資料庫的站點名稱 (sname) 或地址欄位有辦法辨識區域，若無，我們目前先退回不強制過濾 SQL，而是在 Python 端做處理 (如果撈出來的資料有地址的話)。
# 為了穩定，我這裡先用 SQL 撈出基本資料，等一下再想辦法處理區域。

sql_base = """
    SELECT 
        sname, 
        ROUND(AVG(temp)::numeric, 1) AS temp, 
        ROUND(AVG(rain)::numeric, 1) AS rain, 
        ROUND(AVG(avg_rent_count + avg_return_count)::numeric, 1) AS total_activity,
        ROUND(AVG(net_outflow)::numeric, 1) AS net_outflow,
        CASE 
            WHEN AVG(net_outflow) >= 3 THEN 'DEFICIT'
            WHEN AVG(net_outflow) <= -3 THEN 'SURPLUS'
            ELSE 'NORMAL'
        END AS station_status,
        ST_Y(MIN(geom::geometry)) AS lat,
        ST_X(MIN(geom::geometry)) AS lon
    FROM station_risk_status
    WHERE 1=1
"""

conditions = []
params = {}

# 時間處理
if use_date_range:
    conditions.append("record_time >= %(start_date)s AND record_time <= %(end_date)s")
    params['start_date'] = f"{start_date} 00:00:00"
    params['end_date'] = f"{end_date} 23:59:59"

if analysis_mode == "📊 歷史趨勢模式 (看平均狀態)" and use_hour:
    conditions.append("EXTRACT(HOUR FROM record_time) = %(hour)s")
    params['hour'] = target_hour
elif analysis_mode == "🔗 智慧調度模式 (生成搬運工單)":
    conditions.append("EXTRACT(HOUR FROM record_time) = %(hour)s")
    params['hour'] = target_hour

# 行政區處理 (這是一個折衷方案：假設你們的站名 sname 裡面有時候會包含區名，例如 "大安運動中心"；若沒有，這段可能無效，需要你們資料庫加上區域名稱欄位)
# 如果你們確定資料庫有 sarea 欄位，請把 sname LIKE 改成 sarea =
if selected_district != "全台北市 (預設)":
    # 這邊先用站名去模糊比對，算是一個暫時的解法
    conditions.append("sname LIKE %(district)s")
    # 把 "區" 字拿掉增加比對命中率，例如 "大安區" 變 "大安"
    short_district = selected_district.replace("區", "")
    params['district'] = f"%{short_district}%"

# 氣候處理
if weather_temp == "微涼 (< 20°C)": conditions.append("temp < 20")
elif weather_temp == "舒適 (20-26°C)": conditions.append("temp >= 20 AND temp <= 26")
elif weather_temp == "微熱 (26-28°C)": conditions.append("temp >= 26 AND temp <= 28")
elif weather_temp == "炎熱 (28-33°C)": conditions.append("temp > 28 AND temp <= 33")
elif weather_temp == "過熱 (> 33°C)": conditions.append("temp > 33")

if weather_rain == "無雨 (0mm)": conditions.append("rain = 0")
elif weather_rain == "毛毛雨 (0.1-5mm)": conditions.append("rain > 0 AND rain <= 5")
elif weather_rain == "大雨 (5.1-15mm)": conditions.append("rain > 5 AND rain <= 15")
elif weather_rain == "大暴雨 (> 15mm)": conditions.append("rain > 15")

if conditions:
    sql_base += " AND " + " AND ".join(conditions)

sql_base += " GROUP BY sname"

# ==========================================
# 4. 地理距離計算函數 (Haversine)
# ==========================================
def calculate_distance(lat1, lon1, lat2, lon2):
    R = 6371000 
    phi1, phi2 = np.radians(lat1), np.radians(lat2)
    dphi, dlam = np.radians(lat2 - lat1), np.radians(lon2 - lon1)
    a = np.sin(dphi/2)**2 + np.cos(phi1)*np.cos(phi2)*np.sin(dlam/2)**2
    return R * 2 * np.arctan2(np.sqrt(a), np.sqrt(1-a))

# ==========================================
# 5. 執行查詢與邏輯處理
# ==========================================
try:
    with st.spinner('正在計算最佳調度策略...'):
        with engine.connect() as conn:
            all_stations_df = pd.read_sql(sql_base, conn, params=params)
    
    if all_stations_df.empty:
        st.warning(f"⚠️ 在目前設定條件下，找不到歷史資料。")
    else:
        # ----------------------------------------
        # A. 視覺平衡抽樣 (保障紅藍綠名額)
        # ----------------------------------------
        deficit_all = all_stations_df[all_stations_df['station_status'] == 'DEFICIT'].copy()
        surplus_all = all_stations_df[all_stations_df['station_status'] == 'SURPLUS'].copy()
        normal_all = all_stations_df[all_stations_df['station_status'] == 'NORMAL'].copy()

        top_10_deficit = deficit_all.sort_values(by='net_outflow', ascending=False).head(10)
        top_10_surplus = surplus_all.sort_values(by='net_outflow', ascending=True).head(10)
        top_10_normal = normal_all.sort_values(by='total_activity', ascending=False).head(10)

        # ----------------------------------------
        # B. 跨區空間配對演算法 (生成調度工單)
        # ----------------------------------------
        dispatch_tasks = []
        dispatch_sources_df = pd.DataFrame()

        # 只有在「智慧調度模式」才去算路線
        if analysis_mode == "🔗 智慧調度模式 (生成搬運工單)" and not top_10_deficit.empty and not surplus_all.empty:
            for _, d_row in top_10_deficit.iterrows():
                # 計算距離
                dists = surplus_all.apply(
                    lambda s: calculate_distance(d_row['lat'], d_row['lon'], s['lat'], s['lon']), axis=1
                )
                nearest_s_idx = dists.idxmin()
                min_dist = dists[nearest_s_idx]
                
                # 若 3 公里內有滿車站，建立工單
                if min_dist <= 3000:
                    s_row = surplus_all.loc[nearest_s_idx]
                    dispatch_tasks.append({
                        '出發站 (滿車)': s_row['sname'],
                        '目的地 (缺車)': d_row['sname'],
                        '建議搬運量': int(round(min(d_row['net_outflow'], abs(s_row['net_outflow'])))),
                        '預估距離 (m)': round(min_dist, 1),
                        'from_lat': s_row['lat'], 'from_lon': s_row['lon'],
                        'to_lat': d_row['lat'], 'to_lon': d_row['lon']
                    })
            
            dispatch_tasks_df = pd.DataFrame(dispatch_tasks)
            
            # 把救援站加進地圖
            if not dispatch_tasks_df.empty:
                source_snames = dispatch_tasks_df['出發站 (滿車)'].unique()
                dispatch_sources_df = surplus_all[surplus_all['sname'].isin(source_snames)]

        # ----------------------------------------
        # C. 組合地圖最終顯示點位
        # ----------------------------------------
        map_points_df = pd.concat([top_10_deficit, top_10_surplus, top_10_normal, dispatch_sources_df])
        map_points_df = map_points_df.drop_duplicates(subset=['sname']).dropna(subset=['lat', 'lon'])

        # ==========================================
        # 6. UI 標籤頁渲染
        # ==========================================
        tab1, tab2, tab3 = st.tabs(["📊 數據總覽", "🗺️ 空間分佈與調度圖", "📈 調度成效驗證 (After)"])
        
        # --- Tab 1: 數據儀表板 ---
        with tab1:
            st.subheader(f"城市脈動快照")
            col1, col2, col3, col4 = st.columns(4)
            
            col1.metric("🌡️ 平均氣溫", f"{round(all_stations_df['temp'].mean(),1)} °C")
            col2.metric("☔ 平均降雨", f"{round(all_stations_df['rain'].mean(),1)} mm")
            col3.metric("🚨 全域缺車站數", f"{len(deficit_all)} 站")
            col4.metric("📦 全域滿車站數", f"{len(surplus_all)} 站")
            
            # 只在調度模式顯示工單
            if analysis_mode == "🔗 智慧調度模式 (生成搬運工單)" and len(dispatch_tasks) > 0:
                st.markdown("---")
                st.subheader("🚛 即時最佳化調度工單 (Top 10 最危急站點)")
                st.dataframe(
                    dispatch_tasks_df[['出發站 (滿車)', '目的地 (缺車)', '建議搬運量', '預估距離 (m)']].sort_values('預估距離 (m)'), 
                    use_container_width=True
                )
            
            st.markdown("---")
            st.subheader("📋 重點站點清單")
            display_df = map_points_df[['sname', 'total_activity', 'net_outflow', 'station_status']].rename(
                columns={'sname': '站點', 'total_activity': '時周轉量', 'net_outflow': '淨流出量', 'station_status': '狀態'}
            )
            st.dataframe(display_df, use_container_width=True)

        # --- Tab 2: 空間分析地圖 ---
        with tab2:
            st.subheader("台北市 YouBike 空間分佈圖")
            st.caption("🔴 紅色=缺車 | 🔵 藍色=滿車 | 🟢 綠色=熱門正常站" + (" | 🐜 橘色虛線=調度路線" if analysis_mode == "🔗 智慧調度模式 (生成搬運工單)" else ""))

            if not top_10_deficit.empty:
                center_lat, center_lon = top_10_deficit.iloc[0]['lat'], top_10_deficit.iloc[0]['lon']
            else:
                center_lat, center_lon = 25.033, 121.541

            m = folium.Map(location=[center_lat, center_lon], zoom_start=14, tiles="CartoDB positron")

            for _, row in map_points_df.iterrows():
                if row['station_status'] == 'DEFICIT':
                    color, status_text, radius = '#FF4B4B', "🚨 缺車危機", max(6, abs(row['net_outflow']) * 0.8)
                elif row['station_status'] == 'SURPLUS':
                    color, status_text, radius = '#0068C9', "📦 車輛過載", max(6, abs(row['net_outflow']) * 0.8)
                else:
                    color, status_text, radius = '#29B055', "🟢 營運正常", max(4, row['total_activity'] * 0.3)

                popup_html = f"<div style='width:180px;'><b>{row['sname']}</b><br>狀態: {status_text}<br>淨流出: {row['net_outflow']}<br>周轉量: {row['total_activity']}</div>"
                
                folium.CircleMarker(
                    location=[row['lat'], row['lon']], radius=radius, color=color,
                    fill=True, fill_color=color, fill_opacity=0.8,
                    popup=folium.Popup(popup_html, max_width=250)
                ).add_to(m)

            # 畫螞蟻線 (僅限調度模式)
            if analysis_mode == "🔗 智慧調度模式 (生成搬運工單)" and len(dispatch_tasks) > 0:
                for _, task in dispatch_tasks_df.iterrows():
                    AntPath(
                        locations=[[task['from_lat'], task['from_lon']], [task['to_lat'], task['to_lon']]],
                        delay=800, color='orange', pulse_color='black', weight=3, opacity=0.8,
                        tooltip=f"從 {task['出發站 (滿車)']} 搬 {task['建議搬運量']} 輛至 {task['目的地 (缺車)']}"
                    ).add_to(m)

            st_folium(m, width=1200, height=600)
        # --- Tab 3 (核心亮點：雙地圖並排成效驗證) ---
        if analysis_mode == "🔗 智慧調度模式 (生成搬運工單)":
            with tab3:
                st.subheader("📈 調度方案成效模擬")
                st.caption("勾選預計執行的工單，地圖將即時演繹調度後的城市健康度。")
                
                if len(dispatch_tasks) > 0:
                    dispatch_tasks_df['執行此工單'] = True
                    cols = ['執行此工單'] + [c for c in dispatch_tasks_df.columns if c != '執行此工單' and not c.endswith('_lat') and not c.endswith('_lon')]
                    
                    edited_tasks_df = st.data_editor(
                        dispatch_tasks_df[cols],
                        hide_index=True,
                        use_container_width=True,
                        column_config={"執行此工單": st.column_config.CheckboxColumn("執行", default=True)}
                    )

                    # 模擬演算法
                    simulated_df = map_points_df.copy()
                    simulated_df['sim_net_outflow'] = simulated_df['net_outflow']

                    for _, task in edited_tasks_df[edited_tasks_df['執行此工單'] == True].iterrows():
                        simulated_df.loc[simulated_df['sname'] == task['出發站 (滿車)'], 'sim_net_outflow'] += task['建議搬運量']
                        simulated_df.loc[simulated_df['sname'] == task['目的地 (缺車)'], 'sim_net_outflow'] -= task['建議搬運量']

                    simulated_df['sim_status'] = np.where(simulated_df['sim_net_outflow'] >= 3, 'DEFICIT', 
                                                 np.where(simulated_df['sim_net_outflow'] <= -3, 'SURPLUS', 'NORMAL'))

                    # 計算 KPI 數據
                    before_risk = len(simulated_df[simulated_df['station_status'] != 'NORMAL'])
                    after_risk = len(simulated_df[simulated_df['sim_status'] != 'NORMAL'])
                    resolved_risk = before_risk - after_risk
                    resolution_rate = round((resolved_risk / before_risk) * 100, 1) if before_risk > 0 else 0
                    total_moved = edited_tasks_df[edited_tasks_df['執行此工單'] == True]['建議搬運量'].sum()
                    total_distance_m = edited_tasks_df[edited_tasks_df['執行此工單'] == True]['預估距離 (m)'].sum()
                    total_distance_km = round(total_distance_m / 1000, 1)

                    # 顯示大字報 KPI
                    st.markdown("---")
                    kpi1, kpi2, kpi3, kpi4 = st.columns(4)
                    kpi1.metric("🔄 預計調度總車輛", f"{int(total_moved)} 輛")
                    kpi2.metric("🩹 成功修復風險站點", f"{resolved_risk} 站", f"{after_risk} 站仍具風險", delta_color="inverse")
                    kpi3.metric("🎯 區域風險修復率", f"{resolution_rate} %")
                    kpi4.metric("🚚 累積搬運里程", f"{total_distance_km} km")

                    # 🌟 核心修改：建立左右兩個欄位來放地圖 🌟
                    st.markdown("---")
                    st.subheader("🗺️ 調度前後狀態直觀對比 (Side-by-Side)")
                    st.caption("左圖顯示原始危機狀態 | 右圖顯示調度後預期狀態（✨ 標記並常駐名稱者為成功救活的站點）")
                    
                    col_map_before, col_map_after = st.columns(2)
                    
                    # 計算中心點
                    center_lat, center_lon = (top_10_deficit.iloc[0]['lat'], top_10_deficit.iloc[0]['lon']) if not top_10_deficit.empty else (25.033, 121.541)

                    # --- 左邊欄位：調度前地圖 ---
                    with col_map_before:
                        st.markdown("#### 🔍 原始狀態 (Before)")
                        m_before = folium.Map(location=[center_lat, center_lon], zoom_start=14, tiles="CartoDB positron")

                        for _, row in simulated_df.iterrows():
                            if row['station_status'] == 'DEFICIT':
                                color, status_text = '#FF4B4B', "🚨 缺車危機"
                                radius = max(6, abs(row['net_outflow']) * 0.8)
                            elif row['station_status'] == 'SURPLUS':
                                color, status_text = '#0068C9', "📦 車輛過載"
                                radius = max(6, abs(row['net_outflow']) * 0.8)
                            else:
                                color, status_text = '#29B055', "🟢 營運正常"
                                radius = max(4, row['total_activity'] * 0.3)

                            popup_html = f"<div style='width:180px;'><b>{row['sname']}</b><br>狀態: {status_text}<br>淨流出: {row['net_outflow']}</div>"
                            folium.CircleMarker(
                                location=[row['lat'], row['lon']], radius=radius, color=color,
                                fill=True, fill_color=color, fill_opacity=0.8,
                                popup=folium.Popup(popup_html, max_width=250)
                            ).add_to(m_before)
                        
                        # 渲染左地圖 (寬度設 560 剛好符合半邊螢幕)
                        st_folium(m_before, width=560, height=450, key="map_before")

                    # --- 右邊欄位：調度後預期地圖 ---
                    with col_map_after:
                        st.markdown("#### 🚀 預期成效 (After)")
                        m_after = folium.Map(location=[center_lat, center_lon], zoom_start=14, tiles="CartoDB positron")

                        for _, row in simulated_df.iterrows():
                            is_fixed = (row['station_status'] != 'NORMAL') and (row['sim_status'] == 'NORMAL')
                            
                            if row['sim_status'] == 'DEFICIT':
                                color, status_text = '#FF4B4B', "🚨 仍缺車"
                                radius = max(6, abs(row['sim_net_outflow']) * 0.8)
                            elif row['sim_status'] == 'SURPLUS':
                                color, status_text = '#0068C9', "📦 仍滿車"
                                radius = max(6, abs(row['sim_net_outflow']) * 0.8)
                            else:
                                color, status_text = '#00FF00' if is_fixed else '#29B055', ("✨ 成功修復!" if is_fixed else "🟢 營運正常")
                                radius = max(6, abs(row['sim_net_outflow']) * 0.8) if row['sim_status'] != 'NORMAL' else 5

                            popup_html = f"""
                            <div style='width:200px;'>
                                <b>{row['sname']}</b><br>
                                調度前: {row['station_status']} ({row['net_outflow']})<br>
                                <b>預期狀態: {status_text} ({row['sim_net_outflow']})</b>
                            </div>
                            """
                            folium.CircleMarker(
                                location=[row['lat'], row['lon']], radius=radius, color=color,
                                fill=True, fill_color=color, fill_opacity=0.8,
                                popup=folium.Popup(popup_html, max_width=250)
                            ).add_to(m_after)

                            # 成功修復才加上星星和常駐名字
                            if is_fixed:
                                folium.Marker(
                                    location=[row['lat'], row['lon']],
                                    icon=folium.Icon(color='green', icon='star', prefix='fa'),
                                ).add_to(m_after)
                                
                                folium.map.Marker(
                                    location=[row['lat'], row['lon']],
                                    icon=folium.DivIcon(
                                        html=f"""
                                            <div style="font-family: sans-serif; font-size: 13px; font-weight: bold; 
                                                        color: #006400; text-shadow: 2px 2px 4px #FFFFFF, -2px -2px 4px #FFFFFF; 
                                                    white-space: nowrap; transform: translate(15px, -10px);">
                                            ✨ {row['sname']}
                                        </div>
                                    """
                                )
                            ).add_to(m_after)
                        
                        # 渲染右地圖
                        st_folium(m_after, width=560, height=450, key="map_after")
                    
                else:
                    st.info("目前條件下沒有生成調度工單，無法進行模擬。")

except Exception as e:
    st.error(f"❌ 網頁執行失敗。錯誤訊息: {e}")
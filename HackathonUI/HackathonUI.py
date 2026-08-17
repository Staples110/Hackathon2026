import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go 

# Page setup
st.set_page_config(page_title="Syn Bank Share of Wallet Engine", layout="wide")
st.title("Syn Bank: Share of Wallet Intelligence")


# 1. Heatmap Data 
df = pd.read_excel("INSIGHTS.xlsx")
df['entity_name'] = df['entity_name'].str.strip()
products = df['top_opportunity_pillar'].unique().tolist()
clients = df['entity_name'].unique().tolist()

gap_data = df.pivot_table(
    index='entity_name', 
    columns='top_opportunity_pillar', 
    values='wallet_gap_zar_m', 
    fill_value=0 
)

# 2. AI Briefing Notes 
ai_notes = { 
    'MTN Group': "MTN Group (Telecoms): Syn Bank holds a solid 20% wallet share, but there is clear room for growth. Action: Pitch strategic Investment Banking solutions to capture the remaining 80%.",
    'Vodacom Group': "Vodacom Group (Telecoms): This is a massive, largely untapped opportunity where Syn Bank currently holds near-zero share of a huge estimated wallet. Action: Aggressively target as a net-new acquisition for Investment Banking to disrupt competitors.",
    'BHP Group': "BHP Group (Mining): Syn Bank has fully captured the identified Transactional Banking wallet, leaving no current gap in this pillar. Action: Focus on retention for this pillar while exploring cross-selling opportunities in new areas like Trade Finance or Global Markets."
}

# SIDEBAR NAVIGATION
st.sidebar.header("Navigation")
view_mode = st.sidebar.radio("Select View:", ["Overview", "Portfolio Summary", "Client Drill-Down & AI Briefing notes"])


# --- VIEW 1: Overview ---

if view_mode == "Overview":

    if view_mode in ["Overview", "Portfolio Summary"]:
        
        st.header("Portfolio Summary")
        
        st.divider()

        # Loop through each client present in your dataframe
        for index, row in df.iterrows():
            
            st.markdown(f" {row['entity_name']} <span style='font-size:14px; color:gray;'>({row['sector'].capitalize()})</span>", unsafe_allow_html=True)
            
            
            c1, c2, c3 = st.columns(3)
            
            
            est_wallet = f"R {row['Estimated Wallet Size']:,.2f}M"
            captured = f"R {row['Amount_captured_by_synbank']:,.2f}"
            gap = f"R {row['wallet_gap_zar_m']:,.2f}M"
            
            c1.metric(label="Estimated Wallet Size", value=est_wallet)
            c2.metric(label="Captured by Syn Bank", value=captured)
            c3.metric(label="Wallet Gap", value=gap)
           
            st.divider()


            #heatamp ---

        
        pivot_gap = df.pivot(index='entity_name', columns='top_opportunity_pillar', values='wallet_gap_zar_m')
        pivot_share = df.pivot(index='entity_name', columns='top_opportunity_pillar', values='WALLET SHARE')

        gap_data = pd.DataFrame({
            'Investment Banking: Wallet Gap (ZAR m)': pivot_gap.get('Investment Banking'),
            'Investment Banking: Wallet share(ZAR m)': pivot_share.get('Investment Banking'),
            'Transactional Banking: Wallet Gap (ZAR m)': pivot_gap.get('Transactional Banking'),
            'Transactional Banking: Wallet Share (%)': pivot_share.get('Transactional Banking')
        })



        gap_data = gap_data.replace(0.0, np.nan)


        if 'BHP Group' in gap_data.index:
            gap_data.loc['BHP Group', 'Transactional Banking: Wallet Gap (ZAR m)'] = 0.0


        st.subheader("Opportunity Heatmap (ZAR Millions)")
        st.write("Visualizing the revenue gap and share across clients and product pillars.")


        st.dataframe(
            gap_data.style.background_gradient(cmap='YlOrRd', axis=None), 
            use_container_width=True
        )


    if view_mode == "Overview":
        st.divider() 


    st.subheader("3D Revenue Gap Surface")
    st.write("Interactive 3D topology of the opportunity landscape.")

    #Plotly figures
    fig = go.Figure(data=[go.Surface(
        z=gap_data.values,        # The Z-axis heights (the random integers)
        x=gap_data.columns,       # The X-axis labels (Products)
        y=gap_data.index,         # The Y-axis labels (Clients)
        colorscale='YlOrRd'       # Matching your current heatmap colors
    )])

    
    fig.update_layout(
        title='Share of Wallet Topography',
        autosize=True,
        width=700, 
        height=600,
        margin=dict(l=0, r=0, b=0, t=40)
    )

    
    st.plotly_chart(fig, use_container_width=True)

    st.divider()

     # SECTION 2 OF OVERVIEW: CLIENT DRILL-DOWN 

    if view_mode in ["Overview", "Client Drill-Down & AI Briefing notes"]:
        
        st.header("Client Drill-Down")
        st.write("Client selector and AI notes")

     # Client Selection
    
    selected_client = st.sidebar.selectbox("Select a Client:", clients)

    st.subheader(f"Metrics for {selected_client}")

    
    client_data = df[df['entity_name'] == selected_client]

    if not client_data.empty:
        # Extract the first matching row
        client_row = client_data.iloc[0]
        
        # Client Specific KPIs
        c1, c2 = st.columns(2)
        
        
        est_wallet = f"R {client_row['Estimated Wallet Size']:,.2f}M"
        wallet_share = f"{client_row['WALLET SHARE'] * 100:.1f}%"
        
        c1.metric(label="Estimated Client Wallet", value=est_wallet)
        c2.metric(label="Syn Bank Current Share", value=wallet_share)
    else:
        st.warning("No financial data found for this client.")
        
        # Bar chart placeholder for product breakdown
        chart_data = pd.DataFrame(
        np.random.randint(10, 50, size=(len(products), 2)), 
        columns=['Syn Bank Share', 'Competitor Share'], 
        index=products
        )
        st.bar_chart(chart_data)
        
        st.divider()
        
        # AI-Generated Briefing Notes
        st.subheader("Generative AI Briefing Notes")
        if selected_client in ai_notes:
            st.info(ai_notes[selected_client])
        else:
            st.warning("AI briefing generation pending for this client.")








# --- VIEW 2: Portfolio Summary ---

elif view_mode == "Portfolio Summary":
    st.header("Portfolio-Level Summary")
    
    
    st.divider()

  
    for index, row in df.iterrows():
        
        st.markdown(f" {row['entity_name']} <span style='font-size:14px; color:gray;'>({row['sector'].capitalize()})</span>", unsafe_allow_html=True)
            
        
        c1, c2, c3 = st.columns(3)
            
        
        est_wallet = f"R {row['Estimated Wallet Size']:,.2f}M"
        captured = f"R {row['Amount_captured_by_synbank']:,.2f}"
        gap = f"R {row['wallet_gap_zar_m']:,.2f}M"
            
        c1.metric(label="Estimated Wallet Size", value=est_wallet)
        c2.metric(label="Captured by Syn Bank", value=captured)
        c3.metric(label="Wallet Gap", value=gap)
            
        st.divider()
   

     # Heatmap ---

    pivot_gap = df.pivot(index='entity_name', columns='top_opportunity_pillar', values='wallet_gap_zar_m')
    pivot_share = df.pivot(index='entity_name', columns='top_opportunity_pillar', values='WALLET SHARE')

    gap_data = pd.DataFrame({
        'Investment Banking: Wallet Gap (ZAR m)': pivot_gap.get('Investment Banking'),
        'Investment Banking: Wallet share(ZAR m)': pivot_share.get('Investment Banking'),
        'Transactional Banking: Wallet Gap (ZAR m)': pivot_gap.get('Transactional Banking'),
        'Transactional Banking: Wallet Share (%)': pivot_share.get('Transactional Banking')
    })



    gap_data = gap_data.replace(0.0, np.nan)


    if 'BHP Group' in gap_data.index:
        gap_data.loc['BHP Group', 'Transactional Banking: Wallet Gap (ZAR m)'] = 0.0


    st.subheader("Opportunity Heatmap (ZAR Millions)")
    st.write("Visualizing the revenue gap and share across clients and product pillars.")


    st.dataframe(
        gap_data.style.background_gradient(cmap='YlOrRd', axis=None), 
        use_container_width=True
    )

    st.divider()


    st.subheader("3D Revenue Gap Surface")
    st.write("Interactive 3D topology of the opportunity landscape.")

    #Plotly figure
    fig = go.Figure(data=[go.Surface(
        z=gap_data.values,        # The Z-axis heights (the random integers)
        x=gap_data.columns,       # The X-axis labels (Products)
        y=gap_data.index,         # The Y-axis labels (Clients)
        colorscale='YlOrRd'       # Matching your current heatmap colors
    )])

    
    fig.update_layout(
        title='Share of Wallet Topography',
        autosize=True,
        width=700, 
        height=600,
        margin=dict(l=0, r=0, b=0, t=40)
    )

    
    st.plotly_chart(fig, use_container_width=True)





# --- VIEW 3: CLIENT DRILL-DOWNS & AI Notes ---




elif view_mode == "Client Drill-Down & AI Briefing notes":
    st.header("Client Drill-Down & AI Briefing notes")
    
    # Client Selection
    selected_client = st.sidebar.selectbox("Select a Client:", clients)

    st.subheader(f"Metrics for {selected_client}")

    # Filter the dataframe for the selected client
    client_data = df[df['entity_name'] == selected_client]

    if not client_data.empty:
        # Extract the first matching row
        client_row = client_data.iloc[0]
        
        # Client Specific KPIs
        c1, c2 = st.columns(2)
        
        
        est_wallet = f"R {client_row['Estimated Wallet Size']:,.2f}M"
        wallet_share = f"{client_row['WALLET SHARE'] * 100:.1f}%"
        
        c1.metric(label="Estimated Client Wallet", value=est_wallet)
        c2.metric(label="Syn Bank Current Share", value=wallet_share)
    else:
        st.warning("No financial data found for this client.")
    
    # Bar chart placeholder for product breakdown
    chart_data = pd.DataFrame(
        np.random.randint(10, 50, size=(len(products), 2)), 
        columns=['Syn Bank Share', 'Competitor Share'], 
        index=products
    )
    st.bar_chart(chart_data)
    
    st.divider()
    
    # AI-Generated Briefing Notes
    st.subheader("Generative AI Briefing Notes")
    if selected_client in ai_notes:
        st.info(ai_notes[selected_client])
    else:
        st.warning("AI briefing generation pending for this client.")







        
    

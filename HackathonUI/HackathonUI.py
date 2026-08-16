import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go 

# Page setup
st.set_page_config(page_title="Syn Bank Share of Wallet Engine", layout="wide")
st.title("Syn Bank: Share of Wallet Intelligence")

# DUMMY DATA PLACEHOLDERS
# 1. Heatmap Data 
products = ['Transactional', 'Trade Finance', 'Global Markets', 'Investment Banking']
clients = ['Client A', 'Client B', 'Client C', 'Client D', 'Client E']
gap_data = pd.DataFrame(np.random.randint(10, 100, size=(5, 4)), columns=products, index=clients) #Fake Data to be replaced with the real data 

# 2. AI Briefing Notes 
ai_notes = { # load a JSON file or a column from the database where the stored the generated AI text outputs for each client
    'Client A': "AI Briefing: Client A shows a high gap in Trade Finance. Recent JSE SENS announcements indicate expansion into East Africa. Recommend pitching cross-border SWIFT solutions.",
    'Client B': "AI Briefing: High transactional volume observed, but low Global Markets engagement. Potential hedging opportunities identified in recent annual reports.",
    'Client C': "AI Briefing: Competitor X currently holds the majority of the Investment Banking wallet. Recommend leveraging existing transactional relationship to pitch debt restructuring."
}

# SIDEBAR NAVIGATION
st.sidebar.header("Navigation")
view_mode = st.sidebar.radio("Select View:", ["Overview", "Portfolio Summary", "Client Drill-Down"])


# --- VIEW 1: Overview ---

if view_mode == "Overview":

    if view_mode in ["Overview", "Portfolio Summary"]:
        
        st.header("Portfolio Summary")
        

         # High-level KPIs
        col1, col2, col3 = st.columns(3)
        col1.metric(label="Estimated Total Wallet (ZAR)", value="R 5.2B", delta="12% YoY Growth")
        col2.metric(label="Syn Bank Captured Share", value="R 1.1B", delta="-2% vs Competitors", delta_color="inverse")
        col3.metric(label="Total Addressable Gap", value="R 4.1B", delta="High Priority")
        
        st.divider()
        
        # Opportunity Heatmap
        st.subheader("Opportunity Heatmap (ZAR Millions)")
        st.write("Visualizing the revenue gap across clients and product pillars.")
        # Uses Pandas built-in styling for a quick, interactive heatmap
        st.dataframe(gap_data.style.background_gradient(cmap='YlOrRd'), use_container_width=True)

        # VISUAL DIVIDER


    if view_mode == "Overview":
        st.divider() 


    st.subheader("3D Revenue Gap Surface")
    st.write("Interactive 3D topology of the opportunity landscape.")

    # Create the Plotly figure
    fig = go.Figure(data=[go.Surface(
        z=gap_data.values,        # The Z-axis heights (the random integers)
        x=gap_data.columns,       # The X-axis labels (Products)
        y=gap_data.index,         # The Y-axis labels (Clients)
        colorscale='YlOrRd'       # Matching your current heatmap colors
    )])

    # Tweak the layout for a better dashboard fit
    fig.update_layout(
        title='Share of Wallet Topography',
        autosize=True,
        width=700, 
        height=600,
        margin=dict(l=0, r=0, b=0, t=40)
    )

    # Render the interactive plot in Streamlit
    st.plotly_chart(fig, use_container_width=True)

    st.divider()

     # SECTION 2 OF OVERVIEW: CLIENT DRILL-DOWN 

    if view_mode in ["Overview", "Client Drill-Down"]:
        
        st.header("Client Drill-Down")
        st.write("Client selector and AI notes")

     # Client Selection
        selected_client = st.sidebar.selectbox("Select a Client:", list(ai_notes.keys()) + ['Client D', 'Client E'])
        
        st.subheader(f"Metrics for {selected_client}")
        
        # Client Specific KPIs
        c1, c2 = st.columns(2)
        c1.metric(label="Estimated Client Wallet", value="R 450M")
        c2.metric(label="Syn Bank Current Share", value="15%")
        
        # Bar chart placeholder for product breakdown
        chart_data = pd.DataFrame(
            np.random.randint(10, 50, size=(4, 2)), 
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
    
    # High-level KPIs
    col1, col2, col3 = st.columns(3)
    col1.metric(label="Estimated Total Wallet (ZAR)", value="R 5.2B", delta="12% YoY Growth")
    col2.metric(label="Syn Bank Captured Share", value="R 1.1B", delta="-2% vs Competitors", delta_color="inverse")
    col3.metric(label="Total Addressable Gap", value="R 4.1B", delta="High Priority")
    
    st.divider()
    
    # Opportunity Heatmap
    st.subheader("Opportunity Heatmap (ZAR Millions)")
    st.write("Visualizing the revenue gap across clients and product pillars.")
    # Uses Pandas built-in styling for a quick, interactive heatmap
    st.dataframe(gap_data.style.background_gradient(cmap='YlOrRd'), use_container_width=True)

    st.divider()


    st.subheader("3D Revenue Gap Surface")
    st.write("Interactive 3D topology of the opportunity landscape.")

    # Create the Plotly figure
    fig = go.Figure(data=[go.Surface(
        z=gap_data.values,        # The Z-axis heights (the random integers)
        x=gap_data.columns,       # The X-axis labels (Products)
        y=gap_data.index,         # The Y-axis labels (Clients)
        colorscale='YlOrRd'       # Matching your current heatmap colors
    )])

    # Tweak the layout for a better dashboard fit
    fig.update_layout(
        title='Share of Wallet Topography',
        autosize=True,
        width=700, 
        height=600,
        margin=dict(l=0, r=0, b=0, t=40)
    )

    # Render the interactive plot in Streamlit
    st.plotly_chart(fig, use_container_width=True)





# --- VIEW 3: CLIENT DRILL-DOWNS & AI Notes ---




elif view_mode == "Client Drill-Down":
    st.header("Client Drill-Down")
    
    # Client Selection
    selected_client = st.sidebar.selectbox("Select a Client:", list(ai_notes.keys()) + ['Client D', 'Client E'])
    
    st.subheader(f"Metrics for {selected_client}")
    
    # Client Specific KPIs
    c1, c2 = st.columns(2)
    c1.metric(label="Estimated Client Wallet", value="R 450M")
    c2.metric(label="Syn Bank Current Share", value="15%")
    
    # Bar chart placeholder for product breakdown
    chart_data = pd.DataFrame(
        np.random.randint(10, 50, size=(4, 2)), 
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


        
    

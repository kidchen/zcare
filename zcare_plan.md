Architecture & Tech Stack Overview
Development Environment: Local machine (using Claude Code)

Production Environment: Alibaba Cloud Lightweight Application Server (Shanghai Node)

Language/Framework: Python 3.11+ with FastAPI (for Webhooks/APIs) & APScheduler (for cron scheduling)

Integrations:

AMap (AutoNavi / 高德地图) API: Commute routes, real-time traffic jams, weather, transit routing.

Web Scraping / RSS: BeautifulSoup4 + Requests for Hohhot local cultural/event sources & Douban/TV ratings.

WeCom (Enterprise WeChat): Custom Agent API (/cgi-bin/message/send) to push message cards to your parents' WeChat.

Deployment: Docker Compose on Alibaba Cloud Server.

🛠️ Step-by-Step Implementation Plan
Phase 1: Local Development & Script Engine (Claude Code Task)
Task 1.1: Project Setup & Core Client Modules
Deliverable: Directory structure, requirements.txt, configuration manager (config.py with environment variable support).

Modules:

config.py: Storage for API keys (AMap, WeCom CorpID/Secret, default user profiles).

clients/amap_client.py: Wrapper for AMap REST APIs.

get_driving_route(origin, destination): Queries traffic jams, duration, distance.

get_transit_route(origin, destination): Calculates public transport options avoiding frequent transfers.

get_weather(city_code): Fetches current weather & warnings for Hohhot.

clients/wecom_client.py: Class to handle WeCom authentication (fetching access_token) and sending Markdown/Text message cards.

Task 1.2: Feature 1 — Father's Commute Traffic Alert (features/commute.py)
Logic:

Query AMap API for Father's home-to-work driving route at a specified morning time (e.g., 07:30 AM).

Parse road congestion statuses, estimated travel time, traffic delays, and current weather in Hohhot.

Format a concise, warm daily morning message card (e.g., "Dad, morning! Traffic on Route XX is clear today. Est. drive: 22 mins. Weather: Sunny 15°C").

Task 1.3: Feature 2 — Mother's Low-Stress Outing & Transit Planner (features/outing.py)
Logic:

Scrape Hohhot local cultural events/exhibitions or park recommendations (e.g., Inner Mongolia Museum, Dazhao Temple, quiet parks).

Use AMap Transit Routing API from her home location to the destination.

Filter for routes requiring minimal walking and zero/single bus/subway transfers (optimal for low-cardiac stress).

Format a gentle daily itinerary suggestion card with bus numbers and gentle pacing tips.

Task 1.4: Feature 3 — TV Show & Drama Recommendation Engine (features/tv_recommend.py)
Logic:

Scrape trending/high-rated drama lists (e.g., Douban top trending historical/spy/drama series or platform aggregators).

Filter by genre preference (e.g., historical, suspense, retro/年代剧).

Format a weekly "Weekend Drama Pick" message with title, rating, brief synopsis, and streaming platform info.

Task 1.5: Scheduler & Application API (main.py)
Use APScheduler (or FastAPI background tasks) to handle cron jobs:

07:30 AM (China Time): Run Commute Alert for Father.

09:00 AM (China Time): Run Outing Recommendation for Mother.

07:00 PM (Every Friday): Run TV Recommendation for Father.

Phase 2: Deployment & WeCom Setup (Guided by Gemini later)
Once your local code passes tests, we will do the following together:

WeCom Setup:

Register a free WeCom account (no business license required for personal use).

Create a custom Application/Agent inside WeCom admin console.

Obtain CorpID, AgentId, and Secret.

Add your parents as team members so messages arrive directly in their everyday WeChat apps.

Containerization & Deployment:

Package the application into a Docker container (Dockerfile + docker-compose.yml).

Push code to your Shanghai server via Git or SCP over your Tailscale connection.

Run the container in detached mode on Alibaba Cloud.
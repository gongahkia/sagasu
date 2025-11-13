from diagrams import Diagram, Cluster, Edge
from diagrams.onprem.client import User
from diagrams.programming.language import JavaScript
from diagrams.onprem.ci import GithubActions
from diagrams.generic.storage import Storage
from diagrams.generic.blank import Blank
from diagrams.custom import Custom
from diagrams.generic.blank import Blank

# Sagasu 4: Data flow from scraper to static frontend, and how users consume it
with Diagram(
    "Sagasu 4 - Data Flow & Deployment",
    direction="LR",
    show=False,
    filename="architecture_sagasu4",
    outformat="png",
):
    # Main actor
    user = User("User (Browser)")

    # Orchestration (scheduler)
    with Cluster("Orchestration"):
        scheduler = GithubActions("scrape.yml\n(GitHub Actions / Cron)")

    # Backend scraper (Node + Playwright) interacting with external system
    with Cluster("Backend (Node + Playwright)"):
        scraper = JavaScript("scraper-prod.js")
        envfile = Storage("backend/.env\n(SMU credentials)")
        smu_fbs = Custom("SMU FBS\n(Microsoft SSO)", "smu_fbs.png")

    # Data artifacts committed in repo (consumed by frontend)
    with Cluster("Data in Repo"):
        scraped_log = Storage("backend/log/scraped_log.json")
        bookings_log = Storage("backend/log/bookings_log.json")

    # Frontend and hosting
    with Cluster("Frontend (React + Vite)"):
        react_app = Custom("React App", "react.png")
        dev_public = Storage("frontend/public/data/*.json\n(dev only)")

    hosting = Custom("Cloudflare Pages\n(Static Host)", "cloudflare.png")

    # Orchestration triggers backend scraping
    scheduler >> scraper

    # Scraper uses env and logs into external system
    envfile << Edge(label="uses") << scraper >> Edge(label="logs in") >> smu_fbs

    # Scraper writes enhanced JSON artifacts
    scraper >> Edge(label="write") >> scraped_log
    scraper >> Edge(label="write (future)") >> bookings_log

    # Prod data flow: Frontend fetches JSON directly from GitHub (raw)
    scraped_log >> Edge(label="fetch (prod)\nraw.githubusercontent.com") >> react_app

    # Dev data flow: sync script copies files into public folder
    scraped_log >> Edge(label="npm run sync:data", style="dashed") >> dev_public >> Edge(label="fetch (dev)") >> react_app

    # App is deployed as static site and served to users
    react_app >> hosting
    hosting >> user
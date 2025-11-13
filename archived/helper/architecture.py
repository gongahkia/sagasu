from diagrams import Diagram, Cluster
from diagrams.onprem.client import User
from diagrams.programming.language import Python, JavaScript
from diagrams.onprem.ci import GithubActions
from diagrams.generic.storage import Storage
from diagrams.custom import Custom

with Diagram("Sagasu 3 Web App Architecture", direction="LR", show=False):
    # Main actors
    user = User("User")

    with Cluster("GitHub Actions Workflows"):
        scrape = GithubActions("scrape.yml\n(Scrape Workflow)")
        generate = GithubActions("generate.yml\n(Frontend Workflow)")

    with Cluster("Backend"):
        scraper = JavaScript("scraper-prod.js")
        envfile = Storage(".env")
        log = Storage("scraped_log.json")

    with Cluster("Frontend"):
        generator = Python("generate.py")
        html = Storage("index.html")
        facility_icon = Custom("SMU\nFacility Booking\nSystem", "smu.png")

    user >> scrape
    scrape >> scraper
    scraper >> envfile
    scraper >> log
    scrape >> generate  
    generate >> generator
    generator >> log  
    generator >> html
    generator >> facility_icon
    user << html  
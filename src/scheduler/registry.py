from src.core.config import CouncilConfig
from src.core.scraper import BaseScraper
from src.platforms.agile import AgileApplicationsScraper
from src.platforms.appsearchserv import AppSearchServScraper
from src.platforms.ambervalley import AmberValleyScraper
from src.platforms.barnsley import BarnsleyScraper
from src.platforms.bath import BathScraper
from src.platforms.boston import BostonScraper
from src.platforms.fareham import FarehamScraper
from src.platforms.fastweb import FastwebScraper
from src.platforms.jersey import JerseyScraper
from src.platforms.idox import IdoxScraper, IdoxEndExcScraper, IdoxNIScraper, IdoxCrumbScraper
from src.platforms.ni_portal import NIPortalScraper
from src.platforms.northlincs import NorthLincsScraper
from src.platforms.planning_explorer import PlanningExplorerScraper
from src.platforms.planning_register import PlanningRegisterScraper
from src.platforms.salesforce_arcus import SalesforceArcusScraper
from src.platforms.swiftlg import SwiftLGScraper, SwiftLGLabelScraper
from src.platforms.acolnet import CentralBedfordshireScraper
from src.platforms.civica import CivicaScraper
from src.platforms.rochford import RochfordScraper
from src.platforms.stratfordonavon import StratfordOnAvonScraper
from src.platforms.telford import TelfordScraper
from src.platforms.hereford import HerefordScraper
from src.platforms.kensington import KensingtonScraper
from src.platforms.nottinghamshire import NottinghamshireScraper
from src.platforms.ribblevalley import RibbleValleyScraper
from src.platforms.tandridge import TandridgeScraper
from src.platforms.tascomi import TascomiScraper
from src.platforms.kirklees import KirkleesScraper
from src.platforms.southoxon import SouthOxonScraper
from src.platforms.westdunbarton import WestDunbartonScraper
from src.platforms.breckland import BrecklandScraper
from src.platforms.dorset import DorsetScraper
from src.platforms.statmap import StatmapScraper
from src.platforms.hyndburn import NorthgateAssureScraper
from src.platforms.liverpool import LiverpoolScraper
from src.platforms.northgate import NorthgateScraper
from src.platforms.ocella import OcellaScraper
from src.platforms.scillyisles import ScillyIslesScraper
from src.platforms.elmbridge import ElmbridgeScraper
from src.platforms.ipswich import IpswichScraper
from src.platforms.eastsussex import EastSussexScraper


class ScraperRegistry:
    """Maps platform names to scraper classes."""

    def __init__(self):
        self._registry = {
            "appsearchserv": AppSearchServScraper,
            "boston": BostonScraper,
            "idox": IdoxScraper,
            "idox_endexc": IdoxEndExcScraper,
            "idox_ni": IdoxNIScraper,
            "idox_crumb": IdoxCrumbScraper,
            "planning_explorer": PlanningExplorerScraper,
            "swiftlg": SwiftLGScraper,
            "swiftlg_label": SwiftLGLabelScraper,
            "ni_portal": NIPortalScraper,
            "agile": AgileApplicationsScraper,
            "ambervalley": AmberValleyScraper,
            "barnsley": BarnsleyScraper,
            "bath": BathScraper,
            "fareham": FarehamScraper,
            "fastweb": FastwebScraper,
            "northlincs": NorthLincsScraper,
            "salesforce": SalesforceArcusScraper,
            "acolnet": CentralBedfordshireScraper,
            "civica": CivicaScraper,
            "rochford": RochfordScraper,
            "hereford": HerefordScraper,
            "nottinghamshire": NottinghamshireScraper,
            "ribblevalley": RibbleValleyScraper,
            "stratfordonavon": StratfordOnAvonScraper,
            "telford": TelfordScraper,
            "planning_register": PlanningRegisterScraper,
            "tandridge": TandridgeScraper,
            "tascomi": TascomiScraper,
            "jersey": JerseyScraper,
            "kensington": KensingtonScraper,
            "kirklees": KirkleesScraper,
            "southoxon": SouthOxonScraper,
            "whitehorse": SouthOxonScraper,
            "westdunbarton": WestDunbartonScraper,
            "breckland": BrecklandScraper,
            "dorset": DorsetScraper,
            "statmap": StatmapScraper,
            "liverpool": LiverpoolScraper,
            "northgate_assure": NorthgateAssureScraper,
            "ocella": OcellaScraper,
            "northgate": NorthgateScraper,
            "scillyisles": ScillyIslesScraper,
            "elmbridge": ElmbridgeScraper,
            "ipswich": IpswichScraper,
            "eastsussex": EastSussexScraper,
        }

    def get_scraper_class(self, platform):
        if platform not in self._registry:
            raise KeyError(f"No scraper registered for platform: {platform}")
        return self._registry[platform]

    def register(self, platform, scraper_class):
        self._registry[platform] = scraper_class

    def list_platforms(self):
        return list(self._registry.keys())

    def create_scraper(self, config):
        cls = self.get_scraper_class(config.platform)
        return cls(config=config)

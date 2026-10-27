"""FIFA 22 protocol bring-up server, with deliberately explicit compatibility status.

Reference-derived startup and club responses are implemented. Unknown commands
are recorded and the connection is closed so missing native contracts remain visible.
"""
import argparse
import copy
import hashlib
import json
import logging
import os
import random
import re
from pathlib import Path
import secrets
import socketserver
import ssl
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit,parse_qs
from .protocol import Packet, ProtocolError, take_packet, decode, find, integer, text, blob, group, string_map, integer_list, group_map, object_id, group_list, string_list, empty_map
from .state import Club,StateError
from .market import price_bounds,quicksell
from .catalogue import load_catalogue, starter_ids, SOURCE, FALLBACK_SOURCE, ARCHIVE
from .icon_identity import presented_id,original_id,presented_asset
from .sbc_catalog import SBC_CATALOG,SET_BY_ID,CHALLENGE_BY_ID
from . import objectives as SCMP
from . import sqbt

log=logging.getLogger('localfut22')
REFERENCE_COSMETICS=Path(__file__).resolve().parents[1]/'data/content/fifa20'
FIFA22_CONTENT=Path(__file__).resolve().parents[1]/'data/content/fut'
FIFA22_HEADS=Path(__file__).resolve().parents[1]/'data/fut2dheads.big'
ACTIONSHOT_ROOT=FIFA22_HEADS.parent/'actionshots/d'
ACTIONSHOT_MANIFEST=FIFA22_HEADS.parent/'actionshots/actionshots.json'
ACTIONSHOT_INDEX=(json.loads(ACTIONSHOT_MANIFEST.read_text(encoding='utf-8'))
                  if ACTIONSHOT_MANIFEST.is_file() else {})
# Only true edition artwork belongs in FIFA's actionShot layer.  Entries built
# from an ordinary face are useful as source diagnostics, but advertising them
# as action shots makes the client stretch/crop that portrait over both special
# and (when ids collide) base-gold cards.
ACTIONSHOT_IDS={str(resource_id) for resource_id,row in ACTIONSHOT_INDEX.items()
                if isinstance(row,dict) and row.get('source')=='exact'}
TRAIT_FILE=FIFA22_HEADS.parent/'fut22_traits.json'
TRAIT_MASKS=(json.loads(TRAIT_FILE.read_text(encoding='utf-8'))
             if TRAIT_FILE.is_file() else {})
# The action-shot namespace must be stable while its actual DDS manifest is
# unchanged. A random folder on each launch made FIFA retain several URLs for
# the same card; after a match its fixed-size texture cache evicted heads and
# both cards and Store previews started disappearing. A manifest change still
# gets a new namespace automatically.
ACTIONSHOT_SESSION=(hashlib.sha256(ACTIONSHOT_MANIFEST.read_bytes()).hexdigest()[:6]
                    if ACTIONSHOT_MANIFEST.is_file() else '000000')
# Native home/away teamkit asset IDs from the supplied FIFA 22 kit table.
KIT_TEAMS=((1,'Arsenal',1344,1345),(5,'Chelsea',1354,1355),
           (10,'Manchester City',1362,1363),(11,'Manchester United',1364,1365),
           (21,'FC Bayern',1376,1377),(22,'Borussia Dortmund',1380,1381),
           (241,'FC Barcelona',1491,1492),(243,'Real Madrid',1497,1498))

# The pack-opening stage localizes its footer with
# FUT_POA_PLAYER_RARITY_<rareflag>. FIFA 22's downloaded Store dictionary is
# missing several late-promo keys (and labels TOTY only as "Special Item").
# Supply the real FIFA 22 family names through the localization document the
# title already requests when it enters the Store; card shells/rareflags stay
# untouched.
WALKOUT_RARITY_LABELS={
    3:'TEAM OF THE WEEK',5:'TEAM OF THE YEAR',6:'RECORD BREAKER',
    8:'MAN OF THE MATCH',9:'FUTTIES',11:'TEAM OF THE SEASON',12:'ICON',
    16:'FUTTIES WINNER',21:'ONES TO WATCH',22:'RULEBREAKERS',
    25:'PREMIUM SBC',30:'FUT BIRTHDAY',43:'PREMIER LEAGUE POTM',
    46:'EUROPA LEAGUE RTTK',49:'UCL MAN OF THE MATCH',51:'FLASHBACK',
    57:'SHOWDOWN PLUS',58:'SHOWDOWN',64:'TOTY HONOURABLE MENTIONS',
    68:'EUROPA LEAGUE MOTM',70:'UCL TEAM OF THE TOURNAMENT',
    71:'FUTURE STARS',79:'LIGUE 1 POTM',86:'LALIGA POTM',
    90:'PLAYER MOMENTS',91:'OBJECTIVES REWARD',95:'HEADLINERS',
    97:'SHAPESHIFTERS',104:'CONFERENCE LEAGUE RTTK',
    105:'CONFERENCE LEAGUE RTTF',107:'NUMBERS UP',108:'VERSUS ICE',
    110:'SIGNATURE SIGNINGS',114:'SERIE A POTM',115:'EREDIVISIE POTM',
    116:'VERSUS FIRE',118:'WINTER WILDCARDS',124:'UCL ROAD TO THE FINAL',
    125:'EUROPA LEAGUE RTTF',126:'CONFERENCE LEAGUE MOTM',
    130:'SILVER STARS',136:'FANTASY FUT',137:'FUT CAPTAINS',
    143:'LIBERTADORES TEAM OF THE GROUP STAGE',172:'FUT HERO',
    173:'SHAPESHIFTERS HERO',174:'FUTTIES PREMIUM'}

# Legacy FUT development-card identities are stable across FIFA 20/22.  The
# stored item remains authoritative, so applications survive every restart.
POSITION_MODIFIER_EFFECTS={
    5003059:('LWB','LB'),5003060:('LB','LWB'),5003061:('RWB','RB'),
    5003062:('RB','RWB'),5003063:('LM','LW'),5003064:('RM','RW'),
    5003065:('LW','LM'),5003066:('RW','RM'),5003067:('LW','LF'),
    5003068:('RW','RF'),5003069:('LF','LW'),5003070:('RF','RW'),
    5003071:('CM','CAM'),5003072:('CAM','CM'),5003073:('CDM','CM'),
    5003074:('CM','CDM'),5003075:('CAM','CF'),5003076:('CF','CAM'),
    5003077:('CF','ST'),5003078:('ST','CF')}
CONSUMABLE_DEFS={
    5001003:{'cardassetid':7,'cardsubtypeid':201,'rating':80,'rareflag':0,
             'amount':13,'sourceMember':'consumablesContractPlayer'}}
for _index,(_rid,(_from,_to)) in enumerate(POSITION_MODIFIER_EFFECTS.items()):
    CONSUMABLE_DEFS[_rid]={'cardassetid':34,'cardsubtypeid':91+_index,
                           'rating':95,'rareflag':1,'amount':0,
                           'sourceMember':'consumablesPosition',
                           'fromPosition':_from,'toPosition':_to}
for _offset in range(19):
    CONSUMABLE_DEFS[5003095+_offset]={
        'cardassetid':50,'cardsubtypeid':250+_offset,'rating':95,
        'rareflag':0,'amount':0,
        'sourceMember':'consumablesTrainingPlayerPlayStyle'}
for _offset in range(5):
    CONSUMABLE_DEFS[5003114+_offset]={
        'cardassetid':51,'cardsubtypeid':269+_offset,'rating':95,
        'rareflag':0,'amount':0,
        'sourceMember':'consumablesTrainingGkPlayStyle'}

def _development_doc(resource_id):
    meta=CONSUMABLE_DEFS[int(resource_id)]
    return {'resourceId':int(resource_id),'assetId':int(resource_id),
            'cardassetid':int(meta['cardassetid']),
            'cardsubtypeid':int(meta['cardsubtypeid']),
            'rating':int(meta['rating']),'rareflag':int(meta['rareflag']),
            'itemType':'development','itemState':'free','resourceGameYear':2022,
            'discardValue':160 if meta['rareflag'] else 80,
            'owners':1,'untradeable':False,'tradeable':True,'stackCount':1,
            'untradeableCount':0,'amount':int(meta.get('amount',0)),
            str(meta['sourceMember']):1}

DEVELOPMENT_POOL=[_development_doc(rid) for rid in CONSUMABLE_DEFS]
POSITION_POOL=[_development_doc(rid) for rid in POSITION_MODIFIER_EFFECTS]
CLUB_ITEM_POOL=[]
for _team,_name,_home,_away in KIT_TEAMS:
    CLUB_ITEM_POOL.extend((
        {'resourceId':6300000+_team,'definitionId':6300000+_team,
         'assetId':_home,'cardAssetId':_home,
         'itemType':'kit','cardsubtypeid':9,'teamid':_team,'category':2,
         'rating':75,'rareflag':1,'itemState':'free','resourceGameYear':2022,
         'discardValue':80,'owners':1,'untradeable':False,'tradeable':True},
        {'resourceId':6400000+_team,'definitionId':6400000+_team,
         'assetId':_away,'cardAssetId':_away,
         'itemType':'kit','cardsubtypeid':9,'teamid':_team,'category':3,
         'rating':75,'rareflag':1,'itemState':'free','resourceGameYear':2022,
         'discardValue':80,'owners':1,'untradeable':False,'tradeable':True},
        {'resourceId':6000000+_team,'assetId':_team,'cardAssetId':_team,
         'itemType':'custom','cardsubtypeid':11,'teamid':_team,
         'rating':75,'rareflag':1,'itemState':'free','resourceGameYear':2022,
         'discardValue':80,'owners':1,'untradeable':False,'tradeable':True}))
CLUB_ITEM_POOL.append(
    {'resourceId':7000037,'assetId':37,'cardAssetId':37,'itemType':'ball',
     'cardsubtypeid':30,'rating':75,'rareflag':1,'itemState':'free',
     'resourceGameYear':2022,'discardValue':80,'owners':1,
     'untradeable':False,'tradeable':True})
COSMETIC_FILES={
    '/fut/items/images/backgrounds/itemBGs/futitemraritytunables.json':
        'fut/items/images/backgrounds/itemBGs/futitemraritytunables.json',
    '/fut/packs/packopening/packopeningsetting.json':
        'fut/packs/packopening/packopeningsetting.json',
    '/fut/packs/packopening/packopeningconfig.json':
        'fut/packs/packopening/packopeningconfig.json',
    '/fut/playerheads/g4/fut2dheads.big':
        'fut/playerheads/g4/fut2dheads.big',
}
SQBT_KITS=Path(__file__).resolve().parents[1]/'data/fut22_kits.json'
SQBT_ITEM_BASE=800_000_000
SQBT_SQUAD_ID=990_000
SQBT_STADIUM=26
SQBT_STATE_VERSION=2
SQBT_REFRESH_SECONDS=900
# Local store catalogue. Category IDs follow the FIFA 20 purchase-group
# mapping (bronze 1, silver 2, gold 3, special 4). packAssetId values point
# at the pack art served from the supplied content tree.
STORE_PACKS=(
    {'id':1001,'name':'Bronze Pack','description':'Twelve Bronze players.',
     'cost':400,'quantity':12,'minRare':0,'minRating':1,'maxRating':64,'packAssetId':1,'categoryId':1,'group':'bronze'},
    {'id':1002,'name':'Premium Bronze Pack','description':'Twelve higher-tier Bronze players.',
     'cost':750,'quantity':12,'minRare':0,'minRating':55,'maxRating':64,'packAssetId':1,'categoryId':1,'group':'bronze'},
    {'id':1003,'name':'Jumbo Premium Bronze Pack','description':'Twenty-four higher-tier Bronze players.',
     'cost':1500,'quantity':24,'minRare':0,'minRating':55,'maxRating':64,'packAssetId':1,'categoryId':1,'group':'bronze'},
    {'id':1011,'name':'Silver Pack','description':'Twelve Silver players.',
     'cost':2500,'quantity':12,'minRare':0,'minRating':65,'maxRating':74,'packAssetId':2,'categoryId':2,'group':'silver'},
    {'id':1012,'name':'Premium Silver Pack','description':'Twelve higher-tier Silver players.',
     'cost':3750,'quantity':12,'minRare':0,'minRating':69,'maxRating':74,'packAssetId':2,'categoryId':2,'group':'silver'},
    {'id':1013,'name':'Jumbo Premium Silver Pack','description':'Twenty-four higher-tier Silver players.',
     'cost':7500,'quantity':24,'minRare':0,'minRating':69,'maxRating':74,'packAssetId':2,'categoryId':2,'group':'silver'},
    {'id':1021,'name':'Gold Pack','description':'Twelve Gold players, including one Rare.',
     'cost':5000,'quantity':12,'minRare':1,'minRating':75,'packAssetId':3,'categoryId':3,'group':'gold','specialChance':0.03},
    {'id':1022,'name':'Premium Gold Pack','description':'Twelve Gold players, including three Rares.',
     'cost':7500,'quantity':12,'minRare':3,'minRating':75,'packAssetId':3,'categoryId':3,'group':'gold','specialChance':0.05},
    {'id':1023,'name':'Jumbo Premium Gold Pack','description':'Twenty-four Gold players, including seven Rares.',
     'cost':15000,'quantity':24,'minRare':7,'minRating':75,'packAssetId':3,'categoryId':3,'group':'gold','specialChance':0.06},
    {'id':1024,'name':'Gold Players Pack','description':'Twelve Gold players, including one Rare.',
     'cost':12500,'quantity':12,'minRare':1,'minRating':75,'packAssetId':3,'categoryId':3,'group':'gold','specialChance':0.07},
    {'id':1025,'name':'Premium Gold Players Pack','description':'Twelve Gold players, including three Rares.',
     'cost':25000,'quantity':12,'minRare':3,'minRating':75,'packAssetId':3,'categoryId':3,'group':'gold','specialChance':0.10},
    {'id':1026,'name':'Prime Gold Players Pack','description':'Twelve Gold players, including six Rares.',
     'cost':45000,'quantity':12,'minRare':6,'minRating':75,'packAssetId':3,'categoryId':3,'group':'gold','specialChance':0.14},
    {'id':1027,'name':'Rare Players Pack','description':'Twelve Rare Gold players.',
     'cost':50000,'quantity':12,'minRare':12,'minRating':75,'packAssetId':3,'categoryId':3,'group':'gold','specialChance':0.18},
    {'id':1028,'name':'Jumbo Rare Players Pack','description':'Twenty-four Rare Gold players.',
     'cost':100000,'quantity':24,'minRare':24,'minRating':75,'packAssetId':3,'categoryId':3,'group':'gold','specialChance':0.22},
    {'id':1041,'name':'Headliners Players Pack','description':'Three 82+ special-edition players.',
     'cost':15000,'quantity':3,'minRare':3,'minRating':82,'packAssetId':4,'categoryId':4,'group':'special','pool':'special'},
    {'id':1042,'name':'FUT Birthday Players Pack','description':'Five 85+ special-edition players.',
     'cost':35000,'quantity':5,'minRare':5,'minRating':85,'packAssetId':4,'categoryId':4,'group':'special','pool':'special'},
    {'id':1043,'name':'Team of the Season Players Pack','description':'Eight 88+ special-edition players.',
     'cost':75000,'quantity':8,'minRare':8,'minRating':88,'packAssetId':4,'categoryId':4,'group':'special','pool':'special'},
    {'id':1044,'name':'Ultimate Icons Pack','description':'Twelve 90+ special-edition players.',
     'cost':150000,'quantity':12,'minRare':12,'minRating':90,'packAssetId':4,'categoryId':4,'group':'special','pool':'special'},
    {'id':1045,'name':'84+ Promo Player Pick Pack',
     'description':'A choice of one from three 84+ promo players.',
     'cost':50000,'quantity':1,'minRare':1,'minRating':75,'packAssetId':6054,
     'categoryId':4,'group':'special','pool':'base','contentType':'player_pick',
     'playerPick':3,'pickMinRating':84,'skipAnimation':True},
    {'id':1046,'name':'88+ Premium Promo Player Pick Pack',
     'description':'A choice of one from five 88+ promo players.',
     'cost':100000,'quantity':1,'minRare':1,'minRating':75,'packAssetId':6054,
     'categoryId':4,'group':'special','pool':'base','contentType':'player_pick',
     'playerPick':5,'pickMinRating':88,'skipAnimation':True},
    # Reward-only FIFA 22 Upgrade packs. They appear under My Packs only after
    # an SBC grants them; they are not paid Store products.
    {'id':1071,'name':'Two Silver Players Pack','description':'Two Silver players.',
     'cost':0,'quantity':2,'minRare':0,'minRating':65,'maxRating':74,
     'packAssetId':2,'categoryId':2,'group':'silver','rewardOnly':True},
    {'id':1072,'name':'Three Common Gold Players Pack',
     'description':'Three Common Gold players.',
     'cost':0,'quantity':3,'minRare':0,'minRating':75,'packAssetId':3,
     'categoryId':3,'group':'gold','pool':'base_common','rewardOnly':True},
    {'id':1073,'name':'Two Rare Gold Players Pack','description':'Two Rare Gold players.',
     'cost':0,'quantity':2,'minRare':2,'minRating':75,'packAssetId':3,
     'categoryId':3,'group':'gold','rewardOnly':True},
    {'id':1061,'name':'Consumables Pack','description':'Contracts and chemistry styles.',
     'cost':5000,'quantity':10,'minRare':0,'minRating':1,'packAssetId':3,'categoryId':3,'group':'gold','contentType':'consumables','itemPool':DEVELOPMENT_POOL,'skipAnimation':True},
    {'id':1062,'name':'Position Modifiers Pack','description':'Twelve permanent position modifiers.',
     'cost':7500,'quantity':12,'minRare':0,'minRating':1,'packAssetId':3,'categoryId':3,'group':'gold','contentType':'position_modifiers','itemPool':POSITION_POOL,'skipAnimation':True},
    {'id':1063,'name':'Club Items Pack','description':'Persistent kits, badges and balls.',
     'cost':5000,'quantity':12,'minRare':0,'minRating':1,'packAssetId':3,'categoryId':3,'group':'gold','contentType':'club_items','itemPool':CLUB_ITEM_POOL,'skipAnimation':True},
)

def rarity_manifest_with_specials(raw):
    """Serve the supplied full shell manifest without changing its level arrays.

    The title indexes each rarity's CRC arrays by card level. Replacing three
    entries with a single computed CRC made promo shells resolve to NOT FOUND.
    Zero entries are the reference server's representation for local art.
    """
    manifest=Path(__file__).resolve().parents[1]/'data/fut22_raritytunables.json'
    return manifest.read_bytes() if manifest.is_file() else raw

class Runtime:
    def __init__(self,directory,http_port=18222,blaze_port=18221,qos_port=17502,preauth_qos=False,sqbt_native_opponent=True):
        self.directory=Path(directory)
        self.directory.mkdir(parents=True,exist_ok=True)
        self.captures=self.directory/'captures'
        self.captures.mkdir(exist_ok=True)
        self.club=Club(self.directory/'club.sqlite3')
        expected_source=SOURCE if ARCHIVE.is_file() else FALLBACK_SOURCE
        catalogue=load_catalogue()
        self._catalogue_cache=catalogue
        if self.club.catalogue_source()!=expected_source:
            self.club.import_catalogue(catalogue)
            if self.club.status()['items']==0:self.club.starter_squad(starter_ids(catalogue['players']))
            else:
                self.club.migrate_resource_ids({3*(1<<24)+row['assetId']:row['resourceId']
                                                for row in catalogue['players'] if row.get('special')})
                self.club.refresh_item_definitions()
            if ARCHIVE.is_file():
                # Auto-grant only the portrait-verified showcase cards. The
                # supplied editions are flagged special for the Draft captain
                # pool and the store's specials pack, but stay ungranted.
                self.club.grant_specials_once([row['resourceId'] for row in catalogue['players']
                                               if row.get('special')
                                               and row.get('resourceIdStatus')=='futbin-cutout-filename'])
        self.club.repair_market_item_flags()
        repaired_paid_pack_flags=self.club.repair_paid_pack_item_flags()
        self.club.apply_starting_balance(70_000_000,'fut22-70m-v1')
        removed_women=self.club.purge_unsupported_womens_players()
        repaired_club_items=self.club.repair_club_item_resources()
        removed_duplicates=[]
        repaired_unassigned=self.club.repair_unsupported_unassigned_club_items()
        for pack in STORE_PACKS:
            persisted={key:pack[key] for key in
                       ('id','cost','quantity','minRare','minRating')}
            for key in ('pool','itemPool','contentType','maxRating','group',
                        'specialChance'):
                if key in pack:persisted[key]=pack[key]
            self.club.add_pack(persisted)
        self.http_port=http_port
        self.blaze_port=blaze_port
        self.qos_port=qos_port
        self.preauth_qos=preauth_qos
        self.sqbt_native_opponent=sqbt_native_opponent
        self.token=secrets.token_urlsafe(24)
        self.server_source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        self.lock=threading.Lock()
        self.counts={}
        self.census_seen=0
        self.events=self.directory/'events.jsonl'
        self._walkout_sequence=int(time.time())&0x7fffff
        self._preview_item_index={}
        game_directory=os.environ.get('FIFA22_GAME_DIRECTORY','').strip()
        game_root=os.environ.get('LOCALFUT_GAME_ROOT','').strip() or game_directory
        self.game_root=Path(game_root) if game_root else None
        self.walkout_request=(Path(game_directory)/'walkout-request.ini'
                              if game_directory else
                              self.directory/'walkout-request.ini')
        self.clear_walkout_request('startup')
        # This is always an established local FUT club.  Persist the completion
        # marker so changing an equipped badge or kit can never send the retail
        # client back into the first-run kit wizard on a later session.
        self.club.set_meta('onboardingComplete',1)
        if self.club.get_meta('homeKitId') is None:
            self.club.set_meta('homeKitId',6300001)
        if self.club.get_meta('awayKitId') is None:
            self.club.set_meta('awayKitId',6400001)
        if self.club.get_meta('badgeId') is None:
            self.club.set_meta('badgeId',6000001)
        self.club.ensure_cosmetics(self.worn_actives())
        # Cosmetic reconciliation must run before generic duplicate repair.
        # It replaces a synthetic onboarding row with the real owned copy;
        # only unrelated legacy duplicates should be sent to New Items.
        removed_duplicates=self.club.repair_unique_duplicates()
        self.club.sync_player_pick_placeholders()
        self.push_player_pick('startup')
        if removed_women:
            self.event('unsupported_womens_players_removed',count=len(removed_women))
        if repaired_club_items:
            self.event('club_item_resources_repaired',count=repaired_club_items)
        if repaired_paid_pack_flags:
            self.event('paid_pack_tradeability_repaired',count=repaired_paid_pack_flags)
        if removed_duplicates:
            self.event('legacy_unique_duplicates_removed',count=len(removed_duplicates))
        if repaired_unassigned['itemIds']:
            self.event('unsupported_unassigned_club_items_retired',
                       itemIds=repaired_unassigned['itemIds'],
                       refund=repaired_unassigned['refund'])

    def event(self,kind,**fields):
        record={'time':time.time(),'kind':kind,**fields}
        with self.lock:
            with self.events.open('a',encoding='utf-8') as out:out.write(json.dumps(record)+'\n')
            self.counts[kind]=self.counts.get(kind,0)+1
        log.info('%s %s',kind,json.dumps(fields))

    def capture(self,packet,connection_id=None):
        if (packet.component,packet.command)==(10,5):
            with self.lock:
                self.census_seen+=1
                seen=self.census_seen
            if seen>8 and seen%500:
                return [],None,False
        # Use unique filenames, even for simultaneous requests with the same message ID.
        path=self.captures/f'{time.time_ns()}-{secrets.token_hex(4)}-c{packet.component}-k{packet.command}-m{packet.message}.bin'
        path.write_bytes(packet.encode())
        try:
            fields=decode(packet.payload)
            schema=[{'tag':x['tag'],'type':x['type']} for x in fields]
            error=None
        except ProtocolError as exc:
            fields=[];schema=[];error=str(exc)
        self.event('blaze_request',connection_id=connection_id,component=packet.component,command=packet.command,message=packet.message,packet_type=packet.kind,bytes=len(packet.payload),metadata_bytes=len(packet.metadata),header_tail=packet.header_tail.hex(),schema=schema,decode_error=error,capture=path.name)
        return fields,error,True

    def core_config(self):
        base=f'http://127.0.0.1:{self.http_port}'
        return {'AUTH_TYPE':'NUCLEUS','NUCLEUS_LOGIN_ENABLED':'1','ORIGIN_LOGIN_ENABLED':'1',
                'OSDK_AUTH_REQUIRED':'1','OSDK_ONLINE_ENABLED':'1','USE_TOKEN_AUTH':'1',
                'FUT/ENABLED':'1','FUT_URI':base+'/','FUT_RS4_BASE_URL':base+'/',
                'FUTBOOTCFGFILE_URL':base+'/ut/','FUT_AUTH_URL':base+'/ut/auth',
                'FUT_HUB_URL':base+'/ut/game/fifa22/hub',
                'FIFA_POW_URL':base+'/','FIFA_POW_CONTENT_SERVER_URL':base+'/',
                'FIFA_POW_NUCLEUS_PROXY_URL':base+'/','FUTDYNAMICMESSAGES_URL_BASE':base,
                'FUTDYNAMICMESSAGES_CUSTOMURL':f'127.0.0.1:{self.http_port}',
                'ONLINE/FUTDYNAMICMESSAGES_CUSTOMURL':f'127.0.0.1:{self.http_port}',
                'ONLINE/SERVER_RS4':base,'FIFA_RS4_URL':base,
                'OSDK_EASW_AUTH_URL':base+'/easw/auth','OSDK_EASW_REQ_URL':base+'/easw/req',
                'OSDK_EASW_EVENT_URL':base+'/easw/event','OSDK_EASW_MEDIA_URL':base+'/easw/media',
                'ROSTER_URL':base+'/roster','ROSTERUPDATE_URL':base+'/roster',
                'ROUTINGCFGFILE_URL':base+'/routing','CONTENT_URL':base+'/content/',
                'CTL_UPDATE_URL':base+'/ctl','nucleusHost':'127.0.0.1',
                'nucleusPort':str(self.http_port),'nucleusPortSSL':str(self.http_port),
                'nucleusBaseUri':'/','environment':'prod','useNucleusSSL':'0',
                'EASW/ENABLED':'1','ALLOW_OFFLINE':'1','AUTOLOGIN':'0',
                'FUT/FORCE_TUTORIALS':'0',
                'CLIENT_TIMEOUT':'90','REQUEST_TIMEOUT':'80'}

    def local_login(self):
        """Reference-shaped local session for the observed FIFA 22 c1/k10 RPC.

        This is a local identity only. It makes no claim to authenticate an EA
        account or provide a valid EA service token.
        """
        persona=1000000022
        nucleus=1000000022
        details=(text('DSNM','LocalPlayer')+integer('LAST',0)+integer('PID',persona)
                 +integer('PLAT',4)+integer('STAS',2)+integer('XREF',nucleus))
        session=(integer('1CON',0)+integer('BUID',persona)+integer('FRST',0)
                 +text('KEY',self.token)+integer('LLOG',int(time.time()))
                 +text('MAIL','local@localhost')+group('PDTL',details)+integer('UID',nucleus))
        return (integer('CNTX',0)+integer('ERRC',0)+text('SKEY',self.token)
                +integer('ANON',0)+integer('NTOS',0)+group('SESS',session)
                +integer('SPAM',1)+integer('UNDR',0))

    def login_notifications(self):
        persona=1000000022
        nucleus=1000000022
        data=integer('HWFG',0)+group('QDAT',integer('DBPS',0)+integer('NATT',0)+integer('UBPS',0))+integer('UATT',0)
        user=(integer('AID',persona)+integer('ALOC',0)+blob('EXBB',b'')+integer('EXID',0)
              +integer('ID',persona)+text('NAME','LocalPlayer')+text('NASP','cem_ea_id')
              +integer('ORIG',persona)+integer('PIDI',0))
        added=group('DATA',data)+group('USER',user)
        session=(integer('BUID',persona)+object_id('CGID',30722,2,persona)
                 +text('DSNM','LocalPlayer')+text('KEY',self.token)+integer('LAST',int(time.time()))
                 +text('MAIL','local@localhost')+text('NASP','cem_ea_id')+integer('PID',persona)
                 +integer('PLAT',4)+integer('UID',nucleus)+integer('USTP',0)+integer('XREF',nucleus))
        return [(30722,2,added,'UserSessions.UserAdded'),
                (30722,8,session,'UserSessions.LoginSession'),
                (30722,1,group('DATA',data)+integer('USID',persona),'UserSessions.ExtendedDataUpdate'),
                (30722,5,integer('FLGS',3)+integer('ID',persona),'UserSessions.UserUpdated')]

    def telemetry(self):
        return (text('ADRS','127.0.0.1')+integer('ANON',0)+text('DISA','1')
                +text('FILT','')+integer('LOC',1701729619)+text('NOOK','')
                +integer('PORT',9988)+integer('SDLY',0)+text('SESS','')
                +text('SKEY','')+integer('SPCT',0)+text('STIM',''))

    def post_auth(self):
        pss=integer('PORT',0)+integer('RPRT',0)+integer('TIID',0)
        ticker=text('ADRS','127.0.0.1')+integer('PORT',8999)+text('SKEY','')
        options=integer('TMOP',0)+integer('UID',1000000022)
        return (group('PSS',pss)+group('TELE',self.telemetry())
                +group('TICK',ticker)+group('UROP',options))

    def period_ids(self):
        day=int(time.time())//86400
        week=day//7
        month=day//30
        return (integer('DBUF',0)+integer('DHOU',0)+integer('DLY',day)+integer('DRET',7)
                +integer('MBUF',0)+integer('MDAY',1)+integer('MHOU',0)+integer('MLY',month)
                +integer('MRET',12)+integer('WBUF',0)+integer('WDAY',1)+integer('WHOU',0)
                +integer('WLY',week)+integer('WRET',8))

    def entitlements(self,fields):
        requested=find(fields,'GNLS',[])
        if not isinstance(requested,list) or any(not isinstance(name,str) for name in requested):
            raise ProtocolError('Unexpected entitlement grant list')
        # The later reference returns both base-game and online-access grants
        # even when GNLS names only the base-game grant.
        grants=list(dict.fromkeys([*requested,'FIFA22PCBoxContent','FIFA22PCOnlineAccess']))
        if len(grants)>32:raise ProtocolError('Too many entitlement grants')
        entries=[]
        for index,name in enumerate(grants,1):
            if not name.startswith('FIFA22PC'):continue
            entry=(text('DEVI','')+text('GDAY','')+text('GNAM',name)+integer('ID',index)
                   +integer('ISCO',0)+integer('PID',1000000022)+text('PJID','')
                   +integer('PRCA',0)+text('PRID',name)+integer('STAT',1)+integer('STRC',0)
                   +text('TAG','ONLINE_ACCESS')
                   +text('TDAY','')+integer('TYPE',1)+integer('UCNT',0)+integer('VER',0))
            entries.append(entry)
        return group_list('NLST',entries)

    def account_info(self):
        club=self.club.status()
        badge=next((item for item in self.worn_actives()
                    if item.get('itemState')=='activeBadge'),{})
        badge_team=int(badge.get('teamid') or badge.get('teamId') or
                       badge.get('assetId') or 1)
        entry={'year':'2022','platform':'pc','clubName':club['clubName'],
               'established':2022,'assetId':badge_team,
               'badgeAssetId':badge_team,'badgeId':badge_team,
               'teamId':badge_team,'clubAbbr':club['clubAbbr']}
        persona={'personaId':1000000022,'personaName':'LocalPlayer',
                 'userClubList':[entry],'returningUser':1,'hasClub':1,
                 'onboardingComplete':1,'firstTime':False}
        return {'userAccountInfo':{'nucleusId':1000000022,'personas':[persona],
                                   'returningUser':1,'hasClub':1,'onboardingComplete':1},
                'personas':[persona],'returningUser':1,'hasClub':1,'onboardingComplete':1}

    def fut_settings(self):
        # Start with the native reference's account/bootstrap settings. The
        # exact FIFA 22 feature list remains to be learned from the game.
        names=('IsStoreEnabled','cardPackStoreEnabled','pointsPackStoreEnabled',
               'packOpeningAnimationEnabled','squadBuildingChallengeEnabled',
               'isSBCEnabled','sbcEnabled',
               'transferMarketEnabled','squadBattlesEnabled',
               'draftEnabled','draftModeEnabled','futDraftEnabled',
               'enableSeasonalCampaigns','seasonalCampaignsEnabled',
               'objectivesEnabled','seasonObjectivesEnabled',
               'enablePackPreviews','enableShowPackOdds')
        configs=[{'type':name,'value':'1'} for name in names]
        # Confirmed FIFA 22 path: preview action 0x7570 populates the game's
        # own headline-card candidate and then launches the native ceremony.
        configs.append({'type':'packPreviewMaxSlots','value':'9999'})
        return {'configs':configs}

    def kit_item(self,team,name,kit_asset_id,home,index):
        label=f'{name} {"Home" if home else "Away"}'
        # FIFA 22 resolves the shirt art from its native teamkit asset.
        # The selected slot still uses the FUT home/away resource ID.
        resource_id=(6300000 if home else 6400000)+int(team)
        item_id=800000+int(index)
        return {'id':item_id,'itemId':item_id,
                'assetId':int(kit_asset_id),'definitionId':resource_id,
                'resourceId':resource_id,'resourceGameYear':2022,'rating':83,
                'itemType':'kit','cardsubtypeid':9,'cardassetid':35,
                'cardAssetId':int(kit_asset_id),
                'teamid':team,'teamId':team,'rareflag':1,
                'leagueId':0,'category':2 if home else 3,'pile':6,
                'itemState':'free',
                'preferredPosition':'home' if home else 'away',
                'untradeable':True,'tradeable':False,'owners':1,
                'name':label,'displayName':label,'description':label,
                'header':'Kit','biodescription':label,'attributeList':[],
                'statsList':[],'lifetimeStats':[],'discardValue':0}

    def onboarding_kits(self):
        home=[self.kit_item(team,name,h,True,i+1) for i,(team,name,h,a) in enumerate(KIT_TEAMS)]
        away=[self.kit_item(team,name,a,False,101+i) for i,(team,name,h,a) in enumerate(KIT_TEAMS)]
        selected_home=int(self.club.get_meta('homeKitId',6300001))
        selected_away=int(self.club.get_meta('awayKitId',6400001))
        # Equipped packs may contain clubs outside the eight first-run choices.
        # Include those owned kits instead of treating them as an invalid
        # onboarding selection and silently reverting to Arsenal on restart.
        for item in self.club.list_items('club'):
            if str(item.get('itemType','')).lower()!='kit':continue
            resource_id=int(item.get('resourceId') or 0)
            category=int(item.get('category') or 0)
            if category==2 and resource_id==selected_home and not any(
                    int(row['resourceId'])==resource_id for row in home):
                home.append(dict(item,itemId=int(item['id']),pile=6,
                                 itemState='free'))
            elif category==3 and resource_id==selected_away and not any(
                    int(row['resourceId'])==resource_id for row in away):
                away.append(dict(item,itemId=int(item['id']),pile=6,
                                 itemState='free'))
        if selected_home not in {item['resourceId'] for item in home}:
            selected_home=6300001
        if selected_away not in {item['resourceId'] for item in away}:
            selected_away=6400001
        return {'homeItemDataList':home,'awayItemDataList':away,
                'kitsHome':home,'kitsAway':away,'kits':home+away,
                'itemData':home+away,'homeKitId':selected_home,
                'awayKitId':selected_away,'count':len(home)+len(away),
                'success':True,'onboardingComplete':1,'firstTime':False,
                'returningUser':1,'hasClub':1}

    def onboarding_badges(self):
        """Return the complete native badge-selection model.

        FIFA 22 still probes this endpoint when its local tutorial cache is
        stale, even for a returning club.  An empty fallback makes the title
        submit badge ``-1`` and then crash while advancing the wizard.
        """
        badges=[]
        for index,(team,name,_home,_away) in enumerate(KIT_TEAMS,1):
            badge=self.badge_item(team,811000+index)
            badge.update(pile=6,itemState='free',untradeable=True,
                         tradeable=False,name=name,displayName=name)
            badges.append(badge)
        active=next((item for item in self.worn_actives()
                     if item.get('itemState')=='activeBadge'),None)
        if active:
            active_team=int(active.get('teamid') or active.get('teamId') or 0)
            if active_team and not any(int(row.get('teamid') or 0)==active_team
                                       for row in badges):
                badges.append(dict(active,pile=6,itemState='free'))
            badge_resource=int(active.get('resourceId') or
                               (6000000+active_team if active_team else 0))
        else:
            badge_resource=int(badges[0]['resourceId'])
        return {'badgeItemDataList':badges,'badges':badges,'itemData':badges,
                'badgeId':badge_resource,'selectedBadgeId':badge_resource,
                'count':len(badges),'success':True,'onboardingComplete':1,
                'firstTime':False,'returningUser':1,'hasClub':1}

    # --- FIFA 22 Season Objectives -------------------------------------

    def _objective_state(self):
        raw=self.club.get_meta('scmpCampaignFut22V1')
        try:state=json.loads(raw) if raw else {}
        except (TypeError,ValueError):state={}
        if not isinstance(state,dict) or not int(state.get('startTime',0) or 0):
            state={'startTime':int(time.time()),'xp':0,'revision':0,
                   'record':{},'seen':[],
                   'claims':{'objectives':{},'groups':{},'levels':{}}}
            self.club.set_meta('scmpCampaignFut22V1',
                               json.dumps(state,separators=(',',':')))
        for key in ('record','claims'):
            if not isinstance(state.get(key),dict):state[key]={}
        if not isinstance(state.get('seen'),list):state['seen']=[]
        claims=state['claims']
        for key in ('objectives','groups','levels'):
            if not isinstance(claims.get(key),dict):claims[key]={}
        return state

    @staticmethod
    def _objective_metrics(state):
        record=state.get('record',{}) if isinstance(state,dict) else {}
        modes={}
        for mode in ('squad_battles','draft','rivals','champion','generic'):
            row=record.get(mode,{}) if isinstance(record,dict) else {}
            if not isinstance(row,dict):row={}
            modes[mode]={'matches':max(0,int(row.get('matches',0) or 0)),
                         'wins':max(0,int(row.get('wins',0) or 0))}
        # FUT objectives are ordinary FUT/Rivals/Champions play.  Draft and
        # Squad Battles stay isolated and can only advance their own groups.
        modes['fut']={field:sum(modes[mode][field]
                                for mode in ('generic','rivals','champion'))
                      for field in ('matches','wins')}
        modes['all']={field:sum(modes[mode][field]
                                for mode in ('generic','rivals','champion',
                                             'draft','squad_battles'))
                      for field in ('matches','wins')}
        pack_row=record.get('packs',{}) if isinstance(record,dict) else {}
        if not isinstance(pack_row,dict):pack_row={}
        modes['packs']={
            'opened':max(0,int(pack_row.get('opened',0) or 0)),
            'items':max(0,int(pack_row.get('items',0) or 0)),
            'promo':max(0,int(pack_row.get('promo',0) or 0)),
        }
        return modes

    def objectives_campaign(self):
        state=self._objective_state()
        return SCMP.campaign_payload(
            self._objective_metrics(state),state['claims'],
            int(state.get('xp',0) or 0),int(state['startTime']),int(time.time()),
            int(state.get('revision',0) or 0))

    def objectives_live_fields(self,campaign=None):
        current=campaign if isinstance(campaign,dict) else self.objectives_campaign()
        xp=max(0,int(current.get('userXp',0) or 0))
        level=max((int(row['levelId']) for row in current.get('levels',[])
                   if xp>=int(row.get('xpThreshold',0) or 0)),default=1)
        revision=int(current.get('revision',0) or 0)
        # Match/hub DTOs need only the live counters. Embedding three copies of
        # the entire campaign made category responses hundreds of kilobytes
        # and left FIFA's SCMP promise unresolved.
        return {'campaignId':SCMP.CAMPAIGN_ID,'userXp':xp,
                'seasonLevel':level,'objectivesRevision':revision,
                'dynamicObjectivesUpdates':[{
                    'campaignId':SCMP.CAMPAIGN_ID,'userXp':xp,
                    'seasonLevel':level,'revision':revision}]}

    def _with_objectives(self,payload,campaign=None):
        result=dict(payload)
        result.update(self.objectives_live_fields(campaign))
        return result

    def record_objective_match(self,mode,result,event_key):
        mode=str(mode or '').lower()
        normalized=str(result or '').upper()
        if mode not in ('squad_battles','draft','rivals','champion','generic'):
            return False
        if normalized not in ('WIN','DRAW','LOSS'):
            return False
        key=str(event_key or '').strip()
        if not key:return False
        with self.lock:
            state=self._objective_state()
            seen=[str(value) for value in state.get('seen',[])][-511:]
            if key in seen:return False
            record=state.setdefault('record',{})
            row=record.get(mode,{})
            if not isinstance(row,dict):row={}
            row={'matches':max(0,int(row.get('matches',0) or 0))+1,
                 'wins':max(0,int(row.get('wins',0) or 0))+
                        (1 if normalized=='WIN' else 0)}
            record[mode]=row
            gained=SCMP.settled_match_xp(mode,normalized)
            state['xp']=max(0,int(state.get('xp',0) or 0))+gained
            state['revision']=max(0,int(state.get('revision',0) or 0))+1
            state['seen']=[*seen,key]
            self.club.set_meta('scmpCampaignFut22V1',
                               json.dumps(state,separators=(',',':')))
        self.event('objectives_match_progress',mode=mode,result=normalized,
                   eventKey=key,xp=gained,revision=state['revision'],record=row)
        return True

    def record_objective_pack(self,pack,item_count,event_key):
        if not isinstance(pack,dict):return False
        key=f'pack:{str(event_key or "").strip()}'
        if key=='pack:':return False
        with self.lock:
            state=self._objective_state()
            seen=[str(value) for value in state.get('seen',[])][-511:]
            if key in seen:return False
            record=state.setdefault('record',{})
            row=record.get('packs',{})
            if not isinstance(row,dict):row={}
            row={'opened':max(0,int(row.get('opened',0) or 0))+1,
                 'items':max(0,int(row.get('items',0) or 0))+
                         max(0,int(item_count or 0)),
                 'promo':max(0,int(row.get('promo',0) or 0))+
                         (1 if int(pack.get('categoryId',0) or 0)==4 else 0)}
            record['packs']=row
            state['revision']=max(0,int(state.get('revision',0) or 0))+1
            state['seen']=[*seen,key]
            self.club.set_meta('scmpCampaignFut22V1',
                               json.dumps(state,separators=(',',':')))
        self.event('objectives_pack_progress',packId=int(pack.get('id',0) or 0),
                   eventKey=key,revision=state['revision'],record=row)
        return True

    def objective_claim(self,kind,claim_id,option_id=0):
        kind=str(kind)
        claim_id=int(claim_id)
        with self.lock:
            state=self._objective_state()
            claims=state['claims']
            bucket=claims.setdefault(kind,{})
            key=str(claim_id)
            if key in bucket:
                campaign=self.objectives_campaign()
                return 200,{'success':True,'alreadyClaimed':True,'awards':[],
                            **self.objectives_live_fields(campaign)}
            reward=None
            metrics=self._objective_metrics(state)
            if kind=='objectives':
                definition=SCMP.objective_definition(claim_id)
                if definition is None:
                    return 404,{'success':False,'reason':'OBJECTIVE_NOT_FOUND'}
                _,objective=definition
                if SCMP.metric_value(metrics,objective[3])<int(objective[4]):
                    return 409,{'success':False,'reason':'OBJECTIVE_INCOMPLETE'}
                reward=('xp',int(objective[5]),1)
            elif kind=='groups':
                definition=SCMP.group_definition(claim_id)
                if definition is None:
                    return 404,{'success':False,'reason':'GROUP_NOT_FOUND'}
                _,group=definition
                if any(str(row[0]) not in claims['objectives']
                       for row in group['objectives']):
                    return 409,{'success':False,'reason':'GROUP_INCOMPLETE'}
                reward=group['reward']
            elif kind=='levels':
                reward=SCMP.level_reward(claim_id,option_id)
                if reward is None:
                    return 404,{'success':False,'reason':'LEVEL_REWARD_NOT_FOUND'}
                if int(state.get('xp',0) or 0)<SCMP.level_threshold(claim_id):
                    return 409,{'success':False,'reason':'LEVEL_LOCKED'}
            else:
                return 404,{'success':False,'reason':'CLAIM_NOT_FOUND'}
            reward_type,value,count=reward
            if reward_type=='pack' and int(value) not in {row['id'] for row in STORE_PACKS}:
                return 500,{'success':False,'reason':'REWARD_PACK_UNAVAILABLE'}
            bucket[key]=int(time.time())
            if reward_type=='xp':
                state['xp']=max(0,int(state.get('xp',0) or 0))+int(value)*int(count)
            state['revision']=max(0,int(state.get('revision',0) or 0))+1
            self.club.commit_objective_claim(state,reward,
                                             f'FUT 22 Objectives {kind} {claim_id}')
        campaign=self.objectives_campaign()
        self.event('objectives_reward_claimed',claimKind=kind,claimId=claim_id,
                   rewardType=reward_type,value=value,count=count,
                   revision=state['revision'])
        return 200,{'success':True,'alreadyClaimed':False,
                    'awards':[SCMP.reward_wire(reward)],
                    'credits':int(self.club.status()['credits']),
                    'campaign':campaign,
                    **self.objectives_live_fields(campaign)}

    @staticmethod
    def objective_mode_from_payload(payload):
        try:probe=json.dumps(payload,separators=(',',':')).lower()
        except (TypeError,ValueError):probe=''
        if any(token in probe for token in ('division_rivals','division rivals',
                                             'rivals','rivalsv2')):
            return 'rivals'
        if any(token in probe for token in ('fut_champions','fut champions',
                                             'champion','weekend league')):
            return 'champion'
        return 'generic'

    def generic_match_end(self,payload):
        payload=payload if isinstance(payload,dict) else {}
        reason=str(payload.get('reason',payload.get('endReason',
                   payload.get('result',''))) or '').upper()
        if reason in ('QUIT','DNF','FORFEIT','DISCONNECT'):reason='LOSS'
        if reason not in ('WIN','DRAW','LOSS'):
            return {'endReason':'NO_CONTEST',**self.objectives_live_fields()}
        mode=self.objective_mode_from_payload(payload)
        identity=(payload.get('matchReportId') or payload.get('gid')
                  or payload.get('gameId') or payload.get('operationId'))
        if identity:
            key=f'{mode}:{identity}'
        else:
            digest=hashlib.sha256(json.dumps(payload,sort_keys=True,
                                  separators=(',',':')).encode()).hexdigest()[:16]
            key=f'{mode}:{int(time.time())//5}:{digest}'
        self.record_objective_match(mode,reason,key)
        balance=int(self.club.status()['credits'])
        return self._with_objectives({
            'endReason':reason,'won':1 if reason=='WIN' else 0,
            'draw':1 if reason=='DRAW' else 0,'loss':1 if reason=='LOSS' else 0,
            'completionAward':0,'rewardCoins':0,'totalCoins':0,
            'coins':balance,'credits':balance,'objectiveMode':mode,
            'currencies':[{'name':'coins','funds':balance,'finalFunds':balance},
                          {'name':'points','funds':0,'finalFunds':0}]})

    def fut_hub(self):
        mass=self.user_mass_info()
        info=mass['userInfo']
        count=info['clubPlayers']
        return self._with_objectives({'sid':self.token,'sessionId':self.token,
                'personaId':1000000022,'nucleusId':1000000022,
                'clubId':1,'clubName':info['clubName'],'credits':info['credits'],
                'onlineAccess':True,'returningUser':True,
                **self.club.transfer_counts(),
                'clubPlayers':count,'ownedPlayers':count,'playerCount':count,
                'unassignedPileSize':len(self.club.list_items('unassigned')),
                'unopenedPacks':info['unopenedPacks'],
                # The hub's squad consumer also seats the club's worn badge
                # and kits. Keep it consistent with userMassInfo and match.
                'squad':mass['squad'],
                'user':info,'messages':[],'items':[],'news':[],
                'squadBattlesEnabled':True,'squadBattleEnabled':True,
                'squadBattlePoints':self._sqbt_score(),
                'sqbtEvent':self.squad_battles_event_tile(),
                'usLeaderboardAvailable':False})

    def stadium_item(self):
        # Arsenal's native stadium is asset 156 in the FIFA 22 vanity table.
        return {'id':830000,'itemId':830000,'assetId':156,
                'resourceId':6200156,'itemType':'stadium',
                'itemState':'activeStadium','cardsubtypeid':10,
                'cardAssetId':156,'stadiumid':156,'category':4,
                'teamid':1,'teamId':1,'pile':7,'owners':1,
                'untradeable':True,'tradeable':False,
                'name':'StadiumName_156','rating':80,'rareflag':1}

    @staticmethod
    def club_cosmetic_wire(card,*,stadium_slot=False):
        # The club catalogue and stadium slot contain the underlying item.
        # A worn squad kit is a separate view of that item: same item ID,
        # but home/away FUT resourceId and itemType "kit".
        item=dict(card)
        if int(item.get('cardsubtypeid') or 0)==9:
            native=int(item['cardAssetId'])
            item.update(assetId=native,resourceId=native,
                        definitionId=native,itemType='custom')
            item.pop('year',None)
        item['pile']=6
        if stadium_slot:item['itemState']='free'
        return item

    def stadium_document(self):
        # FIFA 22 addresses these first five stadium slots positionally.
        # clientSlotId equals slotId in its stadium reader.
        actives=self.worn_actives()
        layout=(('FUT_STADIUM_BALL',30,0),
                ('FUT_STADIUM_CRESTS',11,0),
                ('FUT_STADIUM_HOME_KIT',9,0),
                ('FUT_STADIUM_AWAY_KIT',9,0),
                ('FUT_STADIUM_ACTIVE_STADIUM',10,1))
        slots=[]
        for order,(name,subtype,area) in enumerate(layout,1):
            item=self.club_cosmetic_wire(actives[order-1],stadium_slot=True)
            slots.append({'slotId':order,'clientSlotId':order,
                          'slotName':name,'sortOrder':order-1,
                          'stadiumArea':area,'vanityTypes':[subtype],
                          'attributes':[],'defaultUnlocked':True,
                          'locked':False,'itemData':item})
        return {'name':self.club.status()['clubName'],
                'lastUsedTierLevel':1,
                'tiers':[{'tier':1,'slots':slots}]}


    def _sqbt_ensure(self):
        if getattr(self,'_sqbt_ready',False):
            return
        catalogue=getattr(self,'_catalogue_cache',None)
        if catalogue is None:
            catalogue=load_catalogue()
            self._catalogue_cache=catalogue
        kits={}
        if SQBT_KITS.is_file():
            kits={int(k):v for k,v in json.loads(
                SQBT_KITS.read_text(encoding='utf-8')).items()}
        sqbt.load_from_catalogue(catalogue['players'],kits)
        self._sqbt_ready=True

    def _sqbt_load_state(self):
        self._sqbt_ensure()
        event_id,start,end=sqbt.event_window()
        raw=self.club.get_meta('sqbtState')
        try:state=json.loads(raw) if raw else {}
        except ValueError:state={}
        if not isinstance(state,dict):state={}
        if int(state.get('v',1))<SQBT_STATE_VERSION:
            state.update({'results':{},'score':0,'played':0,'wins':0,
                          'draws':0,'losses':0,'rotation':0,
                          'v':SQBT_STATE_VERSION})
        if int(state.get('eventId',-1))!=event_id:
            old=int(state.get('eventId',-1))
            if old>=0 and int(state.get('played',0))>0:
                score=int(state.get('score',0))
                state.setdefault('pending',[]).append({
                    'eventId':old,'score':score,
                    'rank':sqbt.user_rank(old,score),
                    'tierLevel':sqbt.tier_level(old,score)})
            state.update({'eventId':event_id,'rotation':0,'score':0,
                          'played':0,'wins':0,'draws':0,'losses':0,
                          'results':{},'selected':-1,
                          'v':SQBT_STATE_VERSION})
            state.setdefault('difficulty',3)
            state.setdefault('pending',[])
        state.setdefault('results',{})
        state.setdefault('pending',[])
        state.setdefault('activeMatch',None)
        state.setdefault('settledMatchStarts',[])
        state.setdefault('lastMatchEndResponse',None)
        # Compat aliases used by older tests / match-end responses.
        state.setdefault('battlePoints',int(state.get('score',0)))
        state.setdefault('matchesPlayed',int(state.get('played',0)))
        state.setdefault('pointMatchesPlayed',int(state.get('played',0)))
        state.setdefault('opponents',state.get('results') or {})
        state['_start']=start
        state['_end']=end
        return state

    def _sqbt_save_state(self,state):
        payload={k:v for k,v in state.items() if not str(k).startswith('_')}
        payload['battlePoints']=int(payload.get('score',0) or 0)
        payload['matchesPlayed']=int(payload.get('played',0) or 0)
        self.club.set_meta('sqbtState',json.dumps(payload,separators=(',',':')))

    def _sqbt_opponents(self):
        state=self._sqbt_load_state()
        results=state.get('results') or {}
        out=[]
        for opponent in sqbt.opponents(int(state['eventId']),
                                       int(state.get('rotation',0))):
            out.append({**opponent,'result':results.get(opponent['key'])})
        return out

    def _sqbt_opponent(self,opponent_id):
        opponent_id=int(opponent_id)
        for opponent in self._sqbt_opponents():
            if int(opponent['opponentId'])==opponent_id:
                return opponent
        return None

    def _sqbt_next_refresh(self):
        state=self._sqbt_load_state()
        now=int(time.time())
        rotation=int(state.get('rotation',0))
        done=all(o.get('result') for o in self._sqbt_opponents())
        if done and rotation<sqbt.MAX_ROTATIONS-1:
            return now-1
        if rotation>=sqbt.MAX_ROTATIONS-1:
            return int(state['_end'])
        return min(int(state['_end']),now+SQBT_REFRESH_SECONDS)

    def _sqbt_kit_items(self,team_id,slot):
        kit=sqbt.kit_of(team_id)
        name=sqbt.team_name(team_id)
        home_asset=int(kit.get('h') or 1362)
        away_asset=int(kit.get('a') or 1363)
        home=dict(self.kit_item(int(team_id),name,home_asset,True,800+slot*2))
        away=dict(self.kit_item(int(team_id),name,away_asset,False,801+slot*2))
        home['id']=home['itemId']=SQBT_ITEM_BASE+slot*100+80
        away['id']=away['itemId']=SQBT_ITEM_BASE+slot*100+81
        home['itemState']='activeHomeKit'
        away['itemState']='activeAwayKit'
        home['pile']=away['pile']=7
        return home,away

    def _sqbt_card(self,asset_id,item_id,team_id):
        rec=sqbt.PLAYERS.get(int(asset_id)) or {}
        item={'id':item_id,'assetId':int(asset_id),'resourceId':int(asset_id),
              'rating':int(rec.get('r') or 0),
              'preferredPosition':str(rec.get('p') or 'CM'),
              'teamid':int(rec.get('t') or team_id),
              'leagueId':int(rec.get('l') or 0),
              'nationId':int(rec.get('u') or 0),
              'rareflag':int(int(rec.get('r') or 0)>=75),
              'attributeArray':list(rec.get('a') or [0]*6),
              'itemType':'player'}
        card=self.player_item_wire(item)
        card.update(id=item_id,itemId=item_id,pile=7,untradeable=True,
                    tradeable=False,itemState='free')
        return card

    def _sqbt_award_rows(self,tier):
        coins=int(tier.get('coins') or 0)
        if not coins:
            return []
        return [{'awardType':'coin','awardValue':coins,'awardCount':1,
                 'value':coins,'assetId':0,'untradeable':False}]

    def _sqbt_prize_tiers(self,event_id):
        floors=sqbt.thresholds(int(event_id))
        rows,cursor=[],0
        for index,(tier,floor) in enumerate(zip(sqbt.PRIZE_TIERS,floors)):
            ceiling=2_147_483_647 if index==0 else floors[index-1]-1
            if index==0:
                start,end,kind=1,sqbt.BOARD_SIZE,'LB_RANKING_TYPE'
            else:
                start=cursor
                cursor+=int(tier.get('percent') or 0)
                end,kind=cursor,''
            rows.append({'tierLevel':int(tier['tierLevel']),
                         'tierStart':start,'tierEnd':end,
                         'tierStartValue':int(floor),
                         'tierEndValue':max(int(floor),int(ceiling)),
                         'tierType':kind,
                         'awards':self._sqbt_award_rows(tier)})
        return rows

    def squad_battles_opponent_wire(self,opponent):
        # RS4:FutSquadBattleOpponentInfo at CardsDLL 0x180383590.
        # id MUST be the slot (0..3). Anything else hung Opponent Select:
        # the hub walks the parsed map by 0..size-1 and a miss inserts an
        # empty node, which FIFA 22 then dereferences.
        team_id=int(opponent['teamId'])
        name=sqbt.team_name(team_id)
        words=''.join(c if c.isalnum() else ' ' for c in name.upper()).split()
        played=opponent.get('result') or None
        slot=int(opponent['opponentId'])
        return {
            'id':slot,'squadId':slot,
            'clubName':name,
            'clubAbbr':(''.join(w[:1] for w in words)[:3] or name[:3].upper() or 'CPU'),
            'rating':int(opponent['rating']),
            # Native FUT uses the 0..99 squad rating here.  Sending 1..5 makes
            # every club render at roughly one-and-a-half stars.
            'starRating':int(opponent['rating']),
            'chemistry':100,
            'formation':sqbt.FORMATION,
            'custom':'[0,0,0,0,0,0,0,0,0,0,0]',
            'badgeAssetId':team_id,
            'nation':'',
            'stadium':SQBT_STADIUM,
            'basePointsWin':sqbt.MATCH_RESULT_WIN,
            'basePointsLoss':sqbt.MATCH_RESULT_LOSS,
            'difficulty':str(int(opponent.get('stars',0)*2)),
            'difficultyName':f"{opponent.get('stars',0):g} STARS",
            'score':int(played['gf']) if played else -1,
            'oppScore':int(played['ga']) if played else -1,
            'penaltyScore':0,'oppPenaltyScore':0,
            'pointsWon':int(played['points']) if played else -1,
            # homeKitData/awayKitData are OBJECTS {category,teamId,year}.
            # Sending a kit id as a scalar froze the attract screen.
            'sqbt2data':{
                'homeKitData':{'category':2,'teamId':team_id,'year':0},
                'awayKitData':{'category':3,'teamId':team_id,'year':0},
                'stadiumAssetId':SQBT_STADIUM,
                'topPlayers':[{'itemData':self._sqbt_card(
                    asset_id,SQBT_ITEM_BASE+slot*100+50+n,team_id)}
                    for n,asset_id in enumerate(sqbt.top_players(team_id))],
            },
        }

    def squad_battles_hub(self,with_opponents=False):
        state=self._sqbt_load_state()
        now=int(time.time())
        if with_opponents and not self.sqbt_native_opponent:
            return self._squad_battles_hub_experimental_rich(
                now,int(state['_start']),int(state['_end']))
        event_id=int(state['eventId'])
        score=int(state.get('score',0))
        modifiers=sqbt.modifiers()
        opponents=self._sqbt_opponents()
        remaining=sum(1 for row in opponents if not row.get('result'))
        return {
            'sqbtEventId':event_id,
            'score':score,
            'rank':sqbt.user_rank(event_id,score),
            'userTierLevel':sqbt.tier_level(event_id,score),
            'gamesPlayed':int(state.get('played',0)),
            'numberOfMatchesPlayed':int(state.get('played',0)),
            'finishedMatches':int(state.get('played',0)),
            'isPrizeAvailable':bool(state.get('pending')),
            'gameModeRestriction':0,
            'currentTime':now,
            'startTime':int(state['_start']),
            'endTime':int(state['_end']),
            'matchResultWin':sqbt.MATCH_RESULT_WIN,
            'matchResultLoss':sqbt.MATCH_RESULT_LOSS,
            'difficultyModifiers':modifiers,
            'winDifficultyModifiers':modifiers,
            'lossDifficultyModifiers':modifiers,
            'nextLBRefreshTimeStamp':int(state['_end']),
            'prizeTiers':self._sqbt_prize_tiers(event_id),
            'squadsRemaining':remaining,
            'remainingMatches':max(0,sqbt.MAX_MATCHES-int(state.get('played',0))),
            'lastMatchUnfinished':False,
            'sqbtOppSquadList':{
                'nextSquadRefreshTimeStamp':self._sqbt_next_refresh(),
                'sqbtOppSquads':[self.squad_battles_opponent_wire(row)
                                 for row in opponents],
            },
        }

    def _sqbt_score(self):
        raw=self.club.get_meta('sqbtState')
        try:return int((json.loads(raw) or {}).get('score',0) or 0)
        except (ValueError,TypeError):return 0

    def squad_battles_event_tile(self):
        # Keep this off the catalogue load path: the FUT hub tile only needs
        # the event window and stored score.
        raw=self.club.get_meta('sqbtState')
        try:state=json.loads(raw) if raw else {}
        except ValueError:state={}
        if not isinstance(state,dict):state={}
        event_id,start,end=sqbt.event_window()
        score=int(state.get('score',0) or 0)
        return {
            'currentTime':int(time.time()),
            'eventStartTime':start,
            'eventEndTime':end,
            'nextSquadRefreshTimeStamp':min(end,int(time.time())+SQBT_REFRESH_SECONDS),
            'unclaimedPrizeWaiting':bool(state.get('pending')),
            'userRank':sqbt.user_rank(event_id,score),
            'userScore':score,
            'userTierLevel':sqbt.tier_level(event_id,score),
        }

    def _squad_battles_hub_experimental_rich(self,now,start,end):
        kit=self.onboarding_kits()
        home=next(item for item in kit['homeItemDataList'] if item['resourceId']==kit['homeKitId'])
        away=next(item for item in kit['awayItemDataList'] if item['resourceId']==kit['awayKitId'])
        club_name='Local Rivals'
        badge_id=10
        top_players=[item['assetId'] for item in sorted(
                     (item for item in self.club.list_items()
                      if item.get('itemType','player')=='player'),
                     key=lambda item:item['rating'],reverse=True)[:3]]
        identity={'clubName':club_name,'clubAbbr':'LRV','displayName':club_name,
                  'teamName':club_name,'ownerName':'LocalRival',
                  'personaName':'LocalRival','nation':14,'location':'ENGLAND',
                  'foundationYear':2021,'est':1633046400,'established':1633046400,
                  'homeKitId':home['resourceId'],'awayKitId':away['resourceId'],
                  'homeKitAssetId':home['assetId'],'awayKitAssetId':away['assetId']}
        opponent={'id':1,'squadId':1,'sqbtOppid':1,'sqbtOpponentSquadId':1,
                  **identity,'badgeAssetId':str(badge_id),'teamId':badge_id,
                  'teamid':badge_id,'rating':68,'chemistry':75,
                  'formation':'f442','difficulty':'SEMI_PRO',
                  'difficultyName':'SEMI_PRO','basePointsWin':550,
                  'basePointsLoss':100,'pointsWon':-1,'score':-1,
                  'oppScore':-1,'penaltyScore':-1,'oppPenaltyScore':-1,
                  'homeKit':home,'awayKit':away,'actives':[home,away],
                  'club':[home,away],'isHomeTeam':True}
        opponent['sqbt2data']={'squadId':1,'teamId':badge_id,'badgeId':badge_id,
                               'badgeAssetId':badge_id,'stadiumAssetId':156,
                               'stadiumId':156,'stadiumName':'Local Stadium',
                               **identity,'homeKit':home['resourceId'],
                               'awayKit':away['resourceId'],
                               'homeKitResourceId':home['resourceId'],
                               'awayKitResourceId':away['resourceId'],
                               'homeKitTeamId':home['teamid'],
                               'awayKitTeamId':away['teamid'],
                               'homeKitItem':home,'awayKitItem':away,
                               'actives':[home,away],'isHomeTeam':True,
                               'topPlayers':top_players}
        event={'id':start,'eventId':start,'competitionId':start,
               'startTime':start,'startTimeStamp':start,'eventStartTime':start,
               'endTime':end,'endTimeStamp':end,'eventEndTime':end,
               'currentTime':now,'crtTime':now,'serverCrtTime':now,
               'serverTimeInSeconds':now,'secondsUntilStart':0,
               'secondsUntilEnd':end-now,'timeUntilStart':0,
               'timeUntilEnd':end-now,'gamesPlayed':0,
               'numberOfMatchesPlayed':0,'matchesPlayed':0,
               'gamesRemaining':30,'remainingMatches':30,
               'squadsRemaining':1,'totalGames':30,'maxMatches':30,
               'maxNumberOfMatches':30,'maxRegularGames':30,
               'squadBattleEnabled':True}
        opponent_squad=dict(self.user_mass_info()['squad'],
                            id=1,squadId=1,name=club_name,squadName=club_name,
                            clubName=club_name,clubAbbr='LRV',teamId=badge_id,
                            teamid=badge_id,badgeAssetId=str(badge_id),
                            rating=68,chemistry=75,actives=[home,away],
                            isHomeTeam=True)
        wins=[0.5,0.6,0.7,1.0,1.6,2.1,2.6]
        losses=[0.2,0.25,0.4,0.5,0.6,0.7,0.7]
        return {'status':'ACTIVE','sqbtEvent':event,'sqbt2data':event,
                'squadBattle':event,'squads':[opponent_squad],
                'squadBattleEnabled':True,'currentTime':now,
                'startTime':start,'endTime':end,
                'sqbtEventId':start,'score':0,'rank':0,'userTierLevel':0,
                'isPrizeAvailable':False,'maxMatches':30,
                'numberOfMatchesPlayed':0,'finishedMatches':0,'gamesPlayed':0,
                'remainingMatches':30,'squadsRemaining':1,
                'lastMatchUnfinished':False,'difficultyModifiers':wins,
                'winDifficultyModifiers':wins,'lossDifficultyModifiers':losses,
                'nextFeatureSquadTimeStamp':end,
                'nextLBRefreshTimeStamp':min(end,now+300),
                'sqbtOppSquadList':{'nextSquadRefreshTimeStamp':now+86400,
                                    'sqbtOppSquads':[opponent]}}

    def squad_battles_hub_native_empty(self,now,start,end):
        wins=[0.5,0.6,0.7,1.0,1.6,2.1,2.6]
        losses=[0.2,0.25,0.4,0.5,0.6,0.7,0.7]
        return {'currentTime':now,'startTime':start,'endTime':end,
                'sqbtEventId':start,'score':0,'rank':0,'userTierLevel':0,
                'isPrizeAvailable':False,'maxMatches':30,
                'numberOfMatchesPlayed':0,'finishedMatches':0,
                'gamesPlayed':0,'remainingMatches':30,
                'squadsRemaining':0,'lastMatchUnfinished':False,
                'difficultyModifiers':wins,'winDifficultyModifiers':wins,
                'lossDifficultyModifiers':losses,
                'nextFeatureSquadTimeStamp':end,
                'nextLBRefreshTimeStamp':min(end,now+300),
                'sqbtOppSquadList':{'nextSquadRefreshTimeStamp':now+86400,
                                    'sqbtOppSquads':[]}}

    def _sqbt_cpu_resource_ids(self,target_rating,exclude):
        # Distinct catalogue players for a CPU 442. FIFA 19: returning the
        # user's own cards makes the loader hang forever.
        from collections import defaultdict
        exclude={int(x) for x in exclude}
        target=int(target_rating) or 70
        by_pos=defaultdict(list)
        catalogue=getattr(self,'_catalogue_cache',None)
        if catalogue is None:
            catalogue=load_catalogue()
            self._catalogue_cache=catalogue
        for row in catalogue['players']:
            asset=int(row['assetId'])
            if asset in exclude or not row.get('teamid'):
                continue
            if not 55<=int(row['rating'])<=92:
                continue
            by_pos[row['preferredPosition']].append(row)
        for group in by_pos.values():
            group.sort(key=lambda r:(abs(int(r['rating'])-target),int(r['resourceId'])))
        order=('GK','RB','CB','CB','LB','RM','CM','CM','LM','ST','ST',
               'GK','RB','CB','CB','LB','RM','CM','CM','LM','ST','ST','ST')
        aliases={'RM':('RM','RW'),'LM':('LM','LW'),'RB':('RB','RWB'),
                 'LB':('LB','LWB'),'CM':('CM','CDM','CAM'),'ST':('ST','CF')}
        selected=[]; used=set()
        for position in order:
            choices=[]
            for pos in aliases.get(position,(position,)):
                choices.extend(by_pos.get(pos) or [])
            choice=next((r for r in choices if r['resourceId'] not in used),None)
            if choice is None:
                choice=next((r for rows in by_pos.values() for r in rows
                             if r['resourceId'] not in used),None)
            if choice is None:
                return None
            used.add(choice['resourceId']); selected.append(choice)
        return selected

    def _sqbt_catalogue_player_card(self,row,item_id):
        item={'id':item_id,'assetId':row['assetId'],'resourceId':row['resourceId'],
              'rating':row['rating'],'preferredPosition':row['preferredPosition'],
              'teamid':row['teamid'],'leagueId':row['leagueId'],
              'nationId':row['nationId'],'rareflag':row['rareflag'],
              'special':bool(row.get('special')),
              'specialVersion':row.get('specialVersion',''),
              'name':row.get('name',''),'lastName':row.get('lastName',''),
              'attributeArray':list(row.get('attributeArray') or [0]*6)}
        card=self.player_item_wire(item)
        card.update(id=item_id,itemId=item_id,pile=7,untradeable=True,
                    tradeable=False,itemState='free')
        return card

    def squad_battles_opponent_squad(self,opponent_id):
        # Root-level FUT squad (same consumer as userMassInfo). FIFA 19: empty
        # manager/actives leaves a Skill Game; personaId must be 0 or the
        # match waits forever for a remote peer; squadType must be DREAM_SQUAD
        # or the client files this as the player's regular squad.
        opponent=self._sqbt_opponent(opponent_id)
        if opponent is None:
            live=self._sqbt_opponents()
            opponent=live[0] if live else None
        if opponent is None:
            return None
        team_id=int(opponent['teamId'])
        slot=int(opponent['opponentId'])
        roster=sqbt.squad_of(team_id)
        players=[]; def_ids=[]
        for index in range(23):
            if index>=len(roster):
                players.append({'index':index,'kitNumber':0,'rank':0})
                continue
            asset_id=roster[index]
            def_ids.append(int(asset_id))
            players.append({
                'index':index,
                'kitNumber':index+1 if index<11 else 0,
                'rank':0,'loyaltyBonus':1,
                'itemData':self._sqbt_card(
                    asset_id,SQBT_ITEM_BASE+slot*100+index,team_id),
            })
        self._sqbt_last_def_ids=def_ids
        state=self._sqbt_load_state()
        state['selected']=slot
        self._sqbt_save_state(state)
        home,away=self._sqbt_kit_items(team_id,slot)
        badge=self.badge_item(team_id,SQBT_ITEM_BASE+slot*100+82)
        badge.update(pile=7,itemState='activeBadge')
        name=sqbt.team_name(team_id)
        return self._finish_squad({
            'id':SQBT_SQUAD_ID+slot,'squadId':SQBT_SQUAD_ID+slot,
            'personaId':0,
            'name':name,'squadName':name,'clubName':name,
            'formation':sqbt.FORMATION,
            'rating':int(opponent['rating']),
            'starRating':int(opponent['rating']),
            'squadType':'DREAM_SQUAD','dreamSquad':True,
            'active':True,'valid':True,'chemistry':100,
            'custom':'[0,0,0,0,0,0,0,0,0,0,0]',
            'teamId':team_id,'clubId':team_id,
            'badgeId':team_id,'badgeAssetId':team_id,
            'players':players,'actives':[self.ball_item(SQBT_ITEM_BASE+slot*100+83),badge,home,away],
            'manager':[self.cpu_manager(slot,team_id)],
            'kicktakers':[],'club':[self.ball_item(SQBT_ITEM_BASE+slot*100+83),badge,home,away],
            'tactics':[],'newSquad':0,'newsquad':0,
        })


    def squad_battles_feature_stats(self):
        # Closed 5-member DTO: FutGetSquadBattleFeaturedServerResponse.
        # Extra nested containers hung the FUT hub in the fifa22work bisection.
        state=self._sqbt_load_state()
        return {'squadName':'','bannerImageName':'',
                'endTimeStamp':int(state['_end']),
                'difficultyBasedWinPoints':list(sqbt.WIN_POINTS),
                'difficultyBasedLossPoints':list(sqbt.LOSS_POINTS)}

    def squad_battles_refresh(self):
        state=self._sqbt_load_state()
        if (all(o.get('result') for o in self._sqbt_opponents())
                and int(state.get('rotation',0))<sqbt.MAX_ROTATIONS-1):
            state['rotation']=int(state.get('rotation',0))+1
            self._sqbt_save_state(state)
        return {'gamesPlayed':int(state.get('played',0)),
                'nextSquadRefreshTimeStamp':self._sqbt_next_refresh(),
                'sqbtOppSquads':[self.squad_battles_opponent_wire(row)
                                 for row in self._sqbt_opponents()]}

    def squad_battles_leaderboard(self,previous=False):
        state=self._sqbt_load_state()
        event_id=int(state['eventId'])-(1 if previous else 0)
        score=0 if previous else int(state.get('score',0))
        if previous:
            for entry in state.get('pending') or []:
                if int(entry['eventId'])==event_id:
                    score=int(entry['score'])
        rows=sqbt.leaderboard(
            event_id,self.club.status().get('clubName','You'),score,
            int(state.get('played',0)),int(state.get('wins',0)),
            int(state.get('draws',0)),int(state.get('losses',0)))
        entries=[]
        for index,row in enumerate(rows):
            entries.append({
                'rank':index+1,'score':int(row['score']),
                'clubName':str(row['name']),
                'badge':0,'est':0,
                'personaId':int(row.get('personaId') or 0),
                'isUser':bool(row.get('isUser')),
            })
        return {'entries':entries,
                'nextLBRefreshTimeStamp':int(state['_end'])}

    def squad_battles_prize(self):
        state=self._sqbt_load_state()
        pending=list(state.get('pending') or [])
        if not pending:
            return {'awardedPrizes':[],'prizesInError':[]}
        paid=[]
        credits=int(self.club.status()['credits'])
        for entry in pending:
            tier=sqbt.tier_of(int(entry['eventId']),int(entry['score']))
            coins=int(tier.get('coins') or 0)
            if coins:
                credits+=coins
                self.club.set_meta('credits',credits)
            paid.append({'eventId':int(entry['eventId']),
                         'rank':int(entry.get('rank') or 0),
                         'tierLevel':int(tier['tierLevel']),
                         'awards':self._sqbt_award_rows(tier)})
        state['pending']=[]
        self._sqbt_save_state(state)
        return {'awardedPrizes':paid,'prizesInError':[]}

    def _sqbt_find_int(self,payload,*keys,default=0):
        if not isinstance(payload,dict):return default
        for key in keys:
            if key in payload:
                try:return int(payload.get(key))
                except (TypeError,ValueError):pass
        return default

    def _sqbt_find_int_nested(self,payload,*keys,default=None):
        if isinstance(payload,dict):
            for key in keys:
                if key in payload:
                    try:return int(payload.get(key))
                    except (TypeError,ValueError):pass
            for value in payload.values():
                found=self._sqbt_find_int_nested(value,*keys,default=None)
                if found is not None:return found
        elif isinstance(payload,list):
            for value in payload:
                found=self._sqbt_find_int_nested(value,*keys,default=None)
                if found is not None:return found
        elif isinstance(payload,str) and payload[:1] in '{[':
            try:parsed=json.loads(payload)
            except ValueError:return default
            return self._sqbt_find_int_nested(parsed,*keys,default=default)
        return default

    def squad_battles_match_start(self,payload=None):
        payload=payload if isinstance(payload,dict) else {}
        state=self._sqbt_load_state()
        # Do NOT read squadId: that is the player's own squad (usually 1).
        opponent_id=self._sqbt_find_int(
            payload,'sqbtOpponentSquadId','sqbtOppid','opponentId',
            'selectedOpponentId','opponentSquadId',default=-1)
        if opponent_id<0 and state.get('activeMatch'):
            opponent_id=int(state['activeMatch'].get('opponentId',-1))
        if opponent_id<0:
            opponent_id=int(state.get('selected',0) or 0)
        difficulty=self._sqbt_find_int(
            payload,'sqbtMatchDifficulty','matchDifficulty','difficulty',
            'difficultyId','gameDifficulty','selectedDifficulty',
            default=int(state.get('difficulty',3) or 3))
        difficulty=sqbt.difficulty_index(difficulty)
        opponent=self._sqbt_opponent(opponent_id)
        opponent_squad=self.squad_battles_opponent_squad(
            opponent_id if opponent is not None else -1)
        if opponent_squad is None:
            return None
        if opponent is None:
            opponent=self._sqbt_opponent(int(opponent_squad.get('id',0))-SQBT_SQUAD_ID) or {}
            opponent_id=int(opponent.get('opponentId',0) or 0)
        user_squad=dict(self.user_mass_info()['squad'])
        actives=self.worn_actives()
        user_squad['actives']=actives
        user_squad['club']=actives
        now=int(time.time())
        state['activeMatch']={
            'opponentId':opponent_id,'difficulty':difficulty,
            'matchStart':now,'gid':self._sqbt_find_int(payload,'gid','gameId',default=0)}
        state['selected']=opponent_id
        state['difficulty']=difficulty
        self._sqbt_save_state(state)
        self.event('sqbt_match_start',opponentId=opponent_id,difficulty=difficulty,
                   matchStart=now,teamId=int(opponent.get('teamId',0) or 0))
        # RS4:FutCreateMatchServerResponse. mismatches MUST be an array (an
        # int hung "create game session"). squad is the PLAYER's XI - sending
        # the opponent here started the match with both sides as the CPU club.
        # FIFA 19 Squad Battles CreateMatch is the same: {squad: HOME,
        # opponentTeamId, startDateTime}. The CPU XI was already fetched from
        # /sqbt/user/opponentsquad/<slot>.
        return {'startDateTime':now,'matchmakingElo':0,'mismatches':[],
                'champs2WinForm':0,'rivalsV2StageId':0,'squad':user_squad}

    def squad_battles_match_end(self,payload=None):
        payload=payload if isinstance(payload,dict) else {}
        state=self._sqbt_load_state()
        active=state.get('activeMatch') or {}
        match_start=int(active.get('matchStart',0) or 0)
        reason=str(payload.get('reason',payload.get('endReason',payload.get('result',''))) or '').upper()
        abandoned=reason in ('QUIT','DNF','FORFEIT','NO_CONTEST','ABANDONED','CANCELLED')
        opponent_id=self._sqbt_find_int(
            payload,'sqbtOpponentSquadId','sqbtOppid','opponentId',
            'selectedOpponentId','opponentSquadId',default=-1)
        if opponent_id<0:
            opponent_id=int(active.get('opponentId',state.get('selected',0)) or 0)
        opponent=self._sqbt_opponent(opponent_id)
        key=(opponent or {}).get('key')
        results=state.setdefault('results',{})
        if match_start and match_start in state.get('settledMatchStarts',[]):
            response=dict(state.get('lastMatchEndResponse') or {'alreadySettled':True})
            response['alreadySettled']=True
            return response
        if key and key in results:
            response=dict(state.get('lastMatchEndResponse') or {'alreadySettled':True})
            response['alreadySettled']=True
            return response
        if abandoned:
            state['activeMatch']=None
            balance=int(self.club.status()['credits'])
            response={'endReason':reason or 'DNF','alreadySettled':False,
                      'squadBattlePoints':0,'battlePoints':int(state.get('score',0)),
                      'score':int(state.get('score',0)),
                      'completionAward':0,'rewardCoins':0,'totalCoins':0,
                      'credits':balance,'coins':balance,
                      'currencies':[{'name':'coins','funds':balance,
                                     'finalFunds':balance},
                                    {'name':'points','funds':0,'finalFunds':0}]}
            state['lastMatchEndResponse']=response
            self._sqbt_save_state(state)
            self.event('sqbt_match_abandoned',opponentId=opponent_id,reason=reason)
            return response
        difficulty=self._sqbt_find_int(
            payload,'sqbtMatchDifficulty','matchDifficulty','difficulty',
            default=int(active.get('difficulty',3) or 3))
        difficulty=sqbt.difficulty_index(difficulty)
        match_data=payload.get('matchData')
        if isinstance(match_data,str) and match_data[:1] in '{[':
            try:match_data=json.loads(match_data)
            except ValueError:match_data=None
        match_data=match_data if isinstance(match_data,dict) else {}
        own_stats=payload.get('myMatchStats') or match_data.get('myMatchStats') or {}
        rival_stats=(payload.get('opponentMatchStats')
                     or match_data.get('opponentMatchStats') or {})
        # Search each team's stats separately. Searching the entire request
        # mistakes the first player's "goals" counter for the match score.
        goals=self._sqbt_find_int(
            payload,'goals','score','userScore','goalsFor','homeGoals','myScore',
            default=None)
        goals_against=self._sqbt_find_int(
            payload,'goalsAgainst','oppScore','opponentScore','opponentGoals',
            'awayGoals',default=None)
        if goals is None:
            goals=self._sqbt_find_int_nested(
                own_stats,'goalsScored','goalsFor','homeGoals','myScore',
                'score','goals',default=None)
        if goals_against is None:
            goals_against=self._sqbt_find_int_nested(
                rival_stats,'goalsScored','opponentGoals','awayGoals',
                'opponentScore','score','goals',default=None)
        if goals is None:
            goals=sum(max(0,int(row.get('goals') or 0))
                      for row in payload.get('items',[])
                      if isinstance(row,dict)) or None
        if goals is None or goals_against is None:
            self.event('sqbt_match_end_no_score',reason=reason,
                       keys=sorted(payload) if isinstance(payload,dict) else [])
            state['activeMatch']=None
            balance=int(self.club.status()['credits'])
            response={'endReason':reason or 'NO_SCORE','alreadySettled':False,
                      'squadBattlePoints':0,'battlePoints':int(state.get('score',0)),
                      'score':int(state.get('score',0)),
                      'completionAward':0,'rewardCoins':0,'totalCoins':0,
                      'credits':balance,'coins':balance,
                      'currencies':[{'name':'coins','funds':balance,
                                     'finalFunds':balance},
                                    {'name':'points','funds':0,'finalFunds':0}]}
            state['lastMatchEndResponse']=response
            self._sqbt_save_state(state)
            return response
        goals=max(0,int(goals)); goals_against=max(0,int(goals_against))
        if not reason:
            reason=sqbt.result_of(goals,goals_against)
        if reason not in ('WIN','DRAW','LOSS'):
            reason=sqbt.result_of(goals,goals_against)
        stars=float((opponent or {}).get('stars',3.0) or 3.0)
        points=sqbt.match_points(difficulty,reason,goals,goals_against,stars)
        coins=0 if reason=='LOSS' and goals==0 else max(150,325+goals*40-goals_against*10)
        status=self.club.status()
        credits=int(status['credits'])+coins
        self.club.set_meta('credits',credits)
        row={'gf':goals,'ga':goals_against,'points':points,
             'difficulty':difficulty,'result':reason,'at':int(time.time())}
        if key:
            results[key]=row
        state['score']=int(state.get('score',0))+points
        state['played']=int(state.get('played',0))+1
        state['battlePoints']=state['score']
        state['matchesPlayed']=state['played']
        state['pointMatchesPlayed']=state['played']
        bucket={'WIN':'wins','DRAW':'draws','LOSS':'losses'}[reason]
        state[bucket]=int(state.get(bucket,0))+1
        rotation=int(state.get('rotation',0))
        live=sqbt.opponents(int(state['eventId']),rotation)
        if live and all(results.get(row['key']) for row in live):
            if rotation<sqbt.MAX_ROTATIONS-1:
                state['rotation']=rotation+1
        if match_start:
            settled=list(state.get('settledMatchStarts',[]))
            settled.append(match_start)
            state['settledMatchStarts']=settled[-20:]
        state['activeMatch']=None
        response={'endReason':reason,'matchDifficulty':difficulty,
                  'opponentTeamId':int((opponent or {}).get('teamId',0) or 0),
                  'completionAward':coins,'rewardCoins':coins,
                  'totalCoins':coins,'coins':credits,'credits':credits,
                  'currencies':[{'name':'coins','funds':credits,
                                 'finalFunds':credits},
                                {'name':'points','funds':0,'finalFunds':0}],
                  'squadBattlePoints':points,'battlePoints':state['score'],
                  'score':state['score'],
                  'won':1 if reason=='WIN' else 0,'draw':1 if reason=='DRAW' else 0,
                  'alreadySettled':False}
        objective_key=(f'squad_battles:{int(state.get("eventId",0) or 0)}:'
                       f'{rotation}:{key or opponent_id}:{match_start}')
        self.record_objective_match('squad_battles',reason,objective_key)
        state['lastMatchEndResponse']=response
        self._sqbt_save_state(state)
        self.event('sqbt_match_end',opponentId=opponent_id,reason=reason,
                   points=points,coins=coins,battlePoints=state['score'])
        return response

    # --- Offline Draft -------------------------------------------------
    # FIFA 19's live-proved CardsDLL contracts (array roots, state literals,
    # purchase-reply = draft-state decoder) applied to FIFA 22's observed
    # /squad/mode/draft/state request. The run persists in club meta so the
    # draft survives a server restart like the rest of the club.

    # Starting-XI position ids follow the chosen formation. The prior fixed
    # left-first table put RB choices in LB tiles (and inverted wings) because
    # FUT's engine order is right-to-left. Reserve slots remain fixed.
    DRAFT_SLOT_POSITIONS=('GK','RB','CB','CB','LB','RM','CM','CM','LM','ST','ST',
                          'GK','RB','CB','LB','RM','CM','LM','CAM','CDM','RW',
                          'LW','ST')
    DRAFT_FORMATIONS=('f442','f433','f4231','f41212','f4312','f4321','f451',
                      'f343','f352','f532')
    DRAFT_FORMATION_POSITIONS={
        'f343':('GK','CB','CB','CB','RM','CM','CM','LM','RW','ST','LW'),
        'f352':('GK','CB','CB','CB','RM','CDM','CDM','LM','CAM','ST','ST'),
        # FIFA 22's f41212 tile is the wide diamond in this client build.
        # The old narrow CM/CM definition made the LM/RM slots display CMs.
        'f41212':('GK','RB','CB','CB','LB','CDM','RM','LM','CAM','ST','ST'),
        'f4231':('GK','RB','CB','CB','LB','CDM','CDM','CAM','CAM','CAM','ST'),
        'f4312':('GK','RB','CB','CB','LB','CM','CM','CM','CAM','ST','ST'),
        'f4321':('GK','RB','CB','CB','LB','CM','CM','CM','RF','LF','ST'),
        'f433':('GK','RB','CB','CB','LB','CM','CM','CM','RW','ST','LW'),
        'f442':('GK','RB','CB','CB','LB','RM','CM','CM','LM','ST','ST'),
        'f451':('GK','RB','CB','CB','LB','RM','CM','LM','CM','CAM','ST'),
        'f532':('GK','RWB','CB','CB','CB','LWB','CM','CM','CM','ST','ST'),
    }
    # The Draft picker starts at Amateur.  Gameplay's native enum still starts
    # at one here, while the round DTO spells PRO as PROFESSIONAL.
    DRAFT_DIFFICULTIES=((1,'AMATEUR',0.75),(2,'SEMIPRO',1.0),
                        (3,'PRO',1.1),(4,'WORLDCLASS',1.25),
                        (5,'LEGENDARY',1.5),(6,'ULTIMATE',1.75))
    # Native reward tiles must never receive the old zero placeholder. These
    # are the same stable tiers used by the repaired FIFA 20 Draft pathway.
    DRAFT_REWARD_COINS=(750,1500,3000,5000,9000)
    # Earned Draft packs use the existing FIFA 22 catalogue IDs so they appear
    # under My Packs and open through the same proven reward-pack path as SBCs.
    # The bundle is deterministic for a run, while higher finishes always move
    # into stronger Gold-player tiers.
    DRAFT_REWARD_PACKS={
        0:((1024,),),
        1:((1022,1024),(1025,)),
        2:((1025,1022),(1024,1024,1022)),
        3:((1026,1023),(1027,1022)),
        4:((1027,1026),(1028,1023)),
    }
    DRAFT_ENTRY_FEE=15000
    DRAFT_SQUAD_ID=900001
    DRAFT_OPPONENT_SQUAD_ID=910001

    def _draft_load_state(self):
        raw=self.club.get_meta('draftState')
        if not raw:return None
        try:state=json.loads(raw)
        except ValueError:return None
        if not isinstance(state,dict):return None
        return state

    def _draft_save_state(self,state):
        self.club.set_meta('draftState',json.dumps(state,separators=(',',':')))

    def _draft_slot_position(self,position_id,formation=''):
        try:pid=int(position_id)
        except (TypeError,ValueError):return 'ANY'
        layout=self.DRAFT_FORMATION_POSITIONS.get(str(formation).lower())
        if layout and 0<=pid<11:return layout[pid]
        if 0<=pid<len(self.DRAFT_SLOT_POSITIONS):
            return self.DRAFT_SLOT_POSITIONS[pid]
        return 'ANY'

    def _draft_position_aliases(self,position):
        return {'RM':('RM','RW'),'LM':('LM','LW'),'RW':('RW','RM'),'LW':('LW','LM'),
                'RF':('RF','RW'),'LF':('LF','LW'),
                'RB':('RB','RWB'),'LB':('LB','LWB'),'RWB':('RWB','RB'),
                'LWB':('LWB','LB'),'CM':('CM','CDM','CAM'),'CDM':('CDM','CM'),
                'CAM':('CAM','CM'),'CF':('CF','ST'),'ST':('ST','CF')}.get(position,(position,))

    def _draft_generate_choices(self,seed,formation=''):
        rng=random.Random(seed)
        catalogue=getattr(self,'_catalogue_cache',None)
        if catalogue is None:
            catalogue=load_catalogue()
            self._catalogue_cache=catalogue
        players=[row for row in catalogue['players'] if int(row['rating'])>=75]
        used_assets=set()
        premium_rarities={5,11,12,51,52,53,66,84,85,86,97}
        def weighted_rows(pool,count,*,captain=False,elite=False):
            """FIFA 20-style Draft luck without a fixed special-card quota.

            Every tile is drawn from one rating/rarity-weighted pool.  This
            deliberately permits any mixture from zero to five specials and
            avoids the visibly scripted first-two-specials/last-three-golds
            pattern introduced by the previous bounded boost.
            """
            candidates=[row for row in pool
                        if int(row['assetId']) not in used_assets]
            rows=[]
            if elite:
                for elite_rating in ((99,98,97,96) if captain else (99,98,97)):
                    tier=[row for row in candidates
                          if int(row['rating'])==elite_rating]
                    if tier:
                        choice=rng.choice(tier)
                        used_assets.add(int(choice['assetId']))
                        rows.append(choice)
                        candidates=[row for row in candidates
                                    if int(row['assetId'])!=int(choice['assetId'])]
                        break
            needed=count-len(rows)
            target_floor=96 if captain else 93
            for rating_floor in range(target_floor,84,-1):
                strong=[row for row in candidates
                        if int(row['rating'])>=rating_floor]
                if len(strong)>=needed:
                    candidates=strong
                    break
            while candidates and len(rows)<count:
                weights=[]
                for row in candidates:
                    rating=int(row['rating'])
                    rarity=int(row.get('rareflag') or 0)
                    rating_bias=max(1.0,(rating-69)**(7.5 if captain else 6.5))
                    special_bias=20.0 if row.get('special') else 1.0
                    if rarity in premium_rarities:special_bias*=3.0
                    if rating>=95:rating_bias*=10.0
                    weights.append(rating_bias*special_bias)
                choice=rng.choices(candidates,weights=weights,k=1)[0]
                used_assets.add(int(choice['assetId']))
                rows.append(choice)
                candidates=[row for row in candidates
                            if int(row['assetId'])!=int(choice['assetId'])]
            return rows
        def choice_item(row,item_id,index):
            item={'id':item_id,'assetId':row['assetId'],'resourceId':row['resourceId'],
                  'rating':row['rating'],'preferredPosition':row['preferredPosition'],
                  'teamid':row['teamid'],'leagueId':row['leagueId'],
                  'nationId':row['nationId'],'rareflag':row.get('rareflag',1),
                  'special':bool(row.get('special')),
                  'specialVersion':row.get('specialVersion',''),
                  'name':row.get('name',''),'lastName':row.get('lastName',''),
                  'attributeArray':list(row.get('attributeArray') or [0]*6)}
            card=self.player_item_wire(item)
            card.update(id=item_id,itemId=item_id,pile=7,itemState='free',
                        untradeable=True,tradeable=False,contract=99,contracts=99)
            return {'index':index,'choiceIndex':index,'resourceId':row['resourceId'],
                    'definitionId':row['resourceId'],'itemData':card}
        choices={'difficulty':[{'index':index,'choiceIndex':index,'difficulty':value,
                                'value':value,'difficultyName':name,
                                'rewardMultiplier':mult}
                               for index,(value,name,mult) in enumerate(self.DRAFT_DIFFICULTIES)],
                 'formation':[{'index':index,'choiceIndex':index,'formation':name,'value':name}
                              for index,name in enumerate(
                                  rng.sample(sorted(self.DRAFT_FORMATIONS),5))],
                 'manager':[]}
        captain_rows=weighted_rows(players,5,captain=True,elite=True)
        choices['captain']=[choice_item(row,self.DRAFT_SQUAD_ID*1000+index,index)
                            for index,row in enumerate(captain_rows)]
        used_assets.update(int(row['assetId']) for row in captain_rows)
        # Match the proven FIFA 20 curve: most runs contain many elite rounds,
        # but the actual five cards are still independently weighted rather
        # than assembled from a fixed special/base recipe.
        elite_slots=set()
        if rng.random()<0.92:
            eligible=list(range(23))
            elite_slots=set(rng.sample(eligible,min(len(eligible),rng.randint(16,22))))
        for slot in range(23):
            position=self._draft_slot_position(slot,formation)
            aliases=self._draft_position_aliases(position)
            # Offer the actual slot position first. The old alias-first pool
            # could put CAM/CDM cards into CM slots and wing-backs into full-
            # back slots even though hundreds of exact FIFA 22 cards exist.
            exact=[row for row in players if row['preferredPosition']==position
                   and int(row['assetId']) not in used_assets]
            compatible=[row for row in players if row['preferredPosition'] in aliases
                        and int(row['assetId']) not in used_assets]
            pool=exact if len(exact)>=5 else compatible
            if len(pool)<5:pool=players
            rows=weighted_rows(pool,5,elite=slot in elite_slots)
            choices[f'player:{slot}']=[choice_item(row,self.DRAFT_SQUAD_ID*1000+100+slot*10+index,index)
                                       for index,row in enumerate(rows)]
        managers=[]
        # Draw from the complete FIFA 22 manager-card table. The previous
        # first-five slice made every Draft offer the same five coaches.
        for index,manager_id in enumerate(self._manager_ids(5,seed)):
            card=self.manager_item(manager_id,
                                   self.DRAFT_SQUAD_ID*1000+900+index)
            managers.append({'index':index,'choiceIndex':index,
                             'resourceId':manager_id,'definitionId':manager_id,
                             'itemData':card})
        choices['manager']=managers
        return choices

    def draft_purchase(self,mode):
        state=self._draft_load_state()
        if mode==0:
            # Retail mode/0 is Online Draft. Keep it unavailable and undebited.
            return self.draft_wire_state(self.draft_state_payload(None))
        if state and str(state.get('draftState','')) not in ('NOT_STARTED','COMPLETE',''):
            return self.draft_wire_state(self.draft_state_payload(state))
        status=self.club.status()
        if int(status['credits'])<self.DRAFT_ENTRY_FEE:
            state={'draftState':'NOT_STARTED'}
            wire=self.draft_wire_state(self.draft_state_payload(None))
            wire['error']='NOT_ENOUGH_CREDITS'
            return wire
        credits=int(status['credits'])-self.DRAFT_ENTRY_FEE
        self.club.set_meta('credits',credits)
        seed=secrets.randbits(48)
        state={'draftState':'PICK_DIFFICULTY','draftId':int(time.time())%100000000,
               'mode':'SINGLE_PLAYER','seed':seed,'wins':0,'losses':0,
               'matchesPlayed':0,'round':1,'difficulty':0,'difficultyName':'',
               'formation':'','captainSlot':-1,'picks':{},'completed':False,
               'choices':self._draft_generate_choices(seed)}
        self._draft_save_state(state)
        self.event('draft_purchase',mode=mode,credits=credits)
        return self.draft_purchase_reply(state)

    def draft_state_payload(self,state):
        """Rich internal state document; the wire publishes only wire members."""
        if not state or str(state.get('draftState','NOT_STARTED'))=='NOT_STARTED':
            return {'draftState':'INVALID','squadState':'INVALID',
                    'stateParam1':'INVALID','stateParam2':'-1',
                    'gamesWonCurrentMatch':0,'roundsInfo':[],
                    'gameModeRestriction':0,
                    'entranceCriteria':{'COINS':self.DRAFT_ENTRY_FEE,'POINTS':300,
                                        'DRAFT_TOKEN':1},
                    'draftId':0,'mode':'SINGLE_PLAYER','draftChampion':False,
                    'draftToken':0,'draftTokens':0,
                    'draftSummary':{'gamesWon':0,'winsRemaining':4,'maxWins':4,'losses':0},
                    'entryFee':{'coins':self.DRAFT_ENTRY_FEE,'draftToken':1}}
        picks=state.get('picks',{})
        active=str(state.get('draftState',''))
        if active=='PICK_DIFFICULTY':
            param1,param2='INVALID','-1'
        elif active=='CAPTAIN_DRAFT':
            param1,param2='PLAYER','0'
        elif active=='PLAYER_DRAFT':
            param1='PLAYER'
            filled={int(key) for key in picks.get('players',{})}
            param2=str(next((slot for slot in range(23) if slot not in filled
                             and slot!=state.get('captainSlot')),23))
        elif active=='MANAGER_DRAFT':
            param1,param2='MANAGER','0'
        else:
            param1,param2='INVALID','23'
        difficulty_name=next((name for value,name,_ in self.DRAFT_DIFFICULTIES
                              if value==int(state.get('difficulty',0) or 0)),'PRO')
        round_difficulty=('PROFESSIONAL' if difficulty_name=='PRO'
                          else difficulty_name)
        rounds_info=[]
        for match in state.get('matches',[]):
            result=str(match.get('result','')).upper()
            rounds_info.append({
                'round':int(match.get('round',len(rounds_info)+1)),
                'difficulty':round_difficulty,
                'score':int(match.get('score',0)),
                'opponentScore':int(match.get('opponentScore',0)),
                'penaltyScore':int(match.get('penaltyScore',0)),
                'opponentPenaltyScore':int(match.get('opponentPenaltyScore',0)),
                'result':result,'matchResult':result,
                'won':1 if result=='WIN' else 0,
                'draw':1 if result=='DRAW' else 0,
                'loss':1 if result=='LOSS' else 0})
        wins=int(state.get('wins',0))
        # FIFA 20/22 uses this current-round row to seed the match settings.
        # Without it the Draft frontend falls back to an unrelated profile
        # difficulty (commonly Ultimate).
        if int(state.get('difficulty',0) or 0) and active not in (
                'PICK_DIFFICULTY','READY_FOR_REWARDS'):
            current_round=wins+1
            if not rounds_info or int(rounds_info[-1].get('round',0))<current_round:
                rounds_info.append({'round':current_round,
                                    'difficulty':round_difficulty,
                                    'opponentId':self.DRAFT_OPPONENT_SQUAD_ID+wins,
                                    'opponentRating':(80,83,86,89)[min(3,wins)],
                                    'score':0,'opponentScore':0,
                                    'penaltyScore':0,'opponentPenaltyScore':0})
        summary={'draftId':state.get('draftId',0),'gamesWon':wins,'wins':wins,
                 'losses':int(state.get('losses',0)),'maxWins':4,
                 'winsRemaining':max(0,4-wins)}
        payload={'draftState':active,'state':active,'squadState':active,
                 'stateParam1':param1,'stateParam2':param2,
                 'gamesWonCurrentMatch':wins,'roundsInfo':rounds_info,
                 'gameModeRestriction':0,
                 'entranceCriteria':{'COINS':self.DRAFT_ENTRY_FEE,'POINTS':300,
                                     'DRAFT_TOKEN':1},
                 'draftId':state.get('draftId',0),'id':state.get('draftId',0),
                 'mode':'SINGLE_PLAYER','draftChampion':wins>=4,
                 'draftToken':0,'draftTokens':0,
                 'formation':state.get('formation',''),
                 'formationid':state.get('formation',''),
                 'captain':int((picks.get('captain') or {}).get('resourceId',0) or 0),
                 'captainid':int((picks.get('captain') or {}).get('resourceId',0) or 0),
                 'difficulty':int(state.get('difficulty',0) or 0),
                 'difficultyName':difficulty_name,
                 'draftSummary':summary,'summary':summary,
                 'squad':self.draft_squad(state),
                 'entryFee':{'coins':self.DRAFT_ENTRY_FEE,'draftToken':1}}
        if active=='READY_FOR_REWARDS':
            payload['awardedPrizes']=self.draft_award_preview(state)
            payload['awardSetId']=min(4,wins)
        return payload

    def draft_wire_state(self,payload):
        """Members decoded by the retail Draft-state DTO (FIFA 19-proved set)."""
        wire={'entranceCriteria':payload['entranceCriteria'],
              'gameModeRestriction':payload.get('gameModeRestriction',0),
              'gamesWonCurrentMatch':payload['gamesWonCurrentMatch'],
              'roundsInfo':payload['roundsInfo'],
              'squadState':payload['squadState'],
              'stateParam1':payload['stateParam1'],
              'stateParam2':payload['stateParam2']}
        if isinstance(payload.get('squad'),dict) and payload['squadState']!='INVALID':
            wire['squad']=payload['squad']
        return wire

    def draft_purchase_reply(self,state):
        payload=self.draft_state_payload(state)
        wire=self.draft_wire_state(payload)
        wire.update({'COINS':int(self.club.status()['credits']),
                     'POINTS':0,'DRAFT_TOKEN':0})
        return wire

    def draft_squad(self,state):
        picks=state.get('picks',{})
        players=[]
        for slot in range(23):
            entry={'index':slot,'loyaltyBonus':1,
                   'kitNumber':slot+1 if slot<11 else 0}
            choice=None
            if int(state.get('captainSlot',-1))==slot:
                choice=picks.get('captain')
            else:
                choice=picks.get('players',{}).get(str(slot))
            if choice and choice.get('itemData'):
                card=dict(choice['itemData'])
                card['pile']=7
                entry['itemData']=card
            players.append(entry)
        starters=[entry['itemData'] for entry in players[:11]
                  if entry.get('itemData')]
        rating=(int(round(sum(int(card.get('rating',0)) for card in starters)/11))
                if len(starters)==11 else 0)
        # The retail FIFA 22 Draft editor displays the manager choice, but its
        # playable-squad parser rejects the transient high-ID manager instance
        # and then reports that no manager is assigned.  Restore the proven
        # owned starter manager used by the working builds.
        manager=self.club_manager()
        manager.update(pile=7,itemState='free')
        return self._finish_squad(
            {'id':self.DRAFT_SQUAD_ID,'squadId':self.DRAFT_SQUAD_ID,
             'personaId':1000000022,'name':'Draft XI','squadName':'Draft XI',
             'formation':state.get('formation') or 'f442',
             'rating':rating,'starRating':rating,'squadType':'REGULAR_SQUAD',
             'active':True,'valid':len(starters)==11,
             'chemistry':100 if len(starters)==11 else 0,'players':players,
             'actives':[],'manager':[manager],'kicktakers':[],
             'club':[],'newSquad':0,'newsquad':0})

    def draft_choices(self,kind,slot):
        state=self._draft_load_state()
        if not state or state.get('draftState')=='NOT_STARTED':
            return {'draftId':0,'choiceType':kind.upper(),'slot':int(slot),
                    'choiceIndex':int(slot),'positionId':int(slot),
                    'positionid':int(slot),'tier':0,'choices':[],'itemData':[]}
        state_kind=str(state.get('draftState',''))
        key_map={'DIFFICULTY':'difficulty','FORMATION':'formation',
                 'CAPTAIN':'captain','MANAGER':'manager'}
        key=key_map.get(kind,f'player:{int(slot)}')
        choices=state.get('choices',{}).get(key,[])
        typemap={'DIFFICULTY':'PICK_DIFFICULTY','FORMATION':'FORMATION_DRAFT',
                 'CAPTAIN':'CAPTAIN_DRAFT','MANAGER':'MANAGER_DRAFT'}
        choice_type=typemap.get(kind,'PLAYER_DRAFT')
        selected=''
        if kind in ('DIFFICULTY','FORMATION','CAPTAIN','MANAGER'):
            picked=state.get('picks',{}).get(kind.lower())
            selected=str(picked.get('resourceId','') if picked else '')
        return {'draftId':state.get('draftId',0),'choiceType':choice_type,
                'slot':int(slot),'choiceIndex':int(slot),'positionId':int(slot),
                'positionid':int(slot),'tier':0,'choices':choices,
                'itemData':[row.get('itemData') for row in choices
                            if isinstance(row,dict) and row.get('itemData')],
                'selectedValue':selected}

    def _draft_pick_row(self,choice,position_id,captain=False):
        row={'resourceId':choice.get('resourceId',0),
             'definitionId':choice.get('definitionId',0),
             'itemData':choice.get('itemData')}
        if captain:row['positionId']=position_id
        return row

    def _draft_apply_slot_order(self,state,raw_order):
        """Persist a Draft XI/bench rearrangement without treating it as Club inventory.

        Draft cards have transient item IDs and deliberately do not belong to the
        normal Club pile.  FIFA nevertheless saves squad 900001 through the same
        HTTP shape used for a regular squad.  Match the submitted IDs back to the
        already-picked Draft rows, move those rows, and retain every unmatched
        card when the client sends a partial array.
        """
        if not isinstance(state,dict) or not isinstance(raw_order,list):
            return False
        picks=state.get('picks')
        if not isinstance(picks,dict):return False
        current=[]
        captain=picks.get('captain')
        if isinstance(captain,dict):
            current.append((int(state.get('captainSlot',-1)),True,captain))
        for raw_slot,row in (picks.get('players') or {}).items():
            if not isinstance(row,dict):continue
            try:slot=int(raw_slot)
            except (TypeError,ValueError):continue
            current.append((slot,False,row))
        if not current:return False

        def identifiers(row):
            item=row.get('itemData') if isinstance(row.get('itemData'),dict) else {}
            values=[]
            for key in ('id','itemId','resourceId','definitionId','_definitionId','assetId'):
                for source in (row,item):
                    try:value=int(source.get(key,0) or 0)
                    except (TypeError,ValueError):value=0
                    if value>0:values.append(value)
            return set(values)

        indexed=[(slot,is_captain,row,identifiers(row))
                 for slot,is_captain,row in current]
        reordered={};used=set()
        for order,submitted in enumerate(raw_order[:23]):
            if isinstance(submitted,dict):
                try:target=int(submitted.get('index',order))
                except (TypeError,ValueError):target=order
                wanted=identifiers(submitted)
            else:
                target=order
                try:value=int(submitted or 0)
                except (TypeError,ValueError):value=0
                wanted={value} if value>0 else set()
            if not (0<=target<23) or not wanted:continue
            match=next((entry for entry in indexed
                        if id(entry[2]) not in used and entry[3]&wanted),None)
            if match is None:continue
            reordered[target]=(match[1],match[2]);used.add(id(match[2]))
        if not used:return False

        # FIFA sometimes submits only the changed starters. Never lose the
        # untouched bench/reserves from a partial request.
        free=[slot for slot in range(23) if slot not in reordered]
        for old_slot,is_captain,row,_ in sorted(indexed,key=lambda entry:entry[0]):
            if id(row) in used:continue
            target=old_slot if 0<=old_slot<23 and old_slot not in reordered else free[0]
            reordered[target]=(is_captain,row)
            if target in free:free.remove(target)

        players={};captain_slot=None
        for slot,(is_captain,row) in sorted(reordered.items()):
            if is_captain:
                captain_slot=slot
                picks['captain']=row
            else:players[str(slot)]=row
        if captain_slot is not None:state['captainSlot']=captain_slot
        picks['players']=players
        return True

    def save_draft_squad(self,payload):
        """Save virtual Draft squad 900001 and return a complete native squad."""
        state=self._draft_load_state()
        if not state or state.get('draftState') not in ('PLAYER_DRAFT','MANAGER_DRAFT',
                                                        'READY_FOR_MATCH'):
            raise StateError('Draft squad is not editable')
        source=payload if isinstance(payload,dict) else {}
        if isinstance(source.get('squad'),dict):source=source['squad']
        players=source.get('players')
        changed=self._draft_apply_slot_order(state,players)
        # Some Draft editor revisions post the compact resource-ID array used by
        # the choose endpoint rather than native player rows.
        if not changed:
            changed=self._draft_apply_slot_order(
                state,source.get('swapPlayerDefIds',source.get('playerDefIds')))
        if not changed:raise StateError('Draft save did not contain known players')
        formation=str(source.get('formation') or '')
        if formation and formation in self.DRAFT_FORMATIONS:
            state['formation']=formation
        self._draft_save_state(state)
        saved=self.draft_squad(state)
        self.event('draft_squad_saved',filled=sum(
            1 for row in saved.get('players',[]) if row.get('itemData')),
            captainSlot=int(state.get('captainSlot',-1)))
        return saved

    def draft_choose(self,payload):
        payload=payload if isinstance(payload,dict) else {}
        state=self._draft_load_state()
        if not state or state.get('draftState') in ('NOT_STARTED','','COMPLETE',
                                                    'READY_FOR_REWARDS'):
            wire=self.draft_wire_state(self.draft_state_payload(state))
            wire['error']='NO_ACTIVE_DRAFT'
            return wire
        try:choice_index=int(payload.get('choiceIndex',0) or 0)
        except (TypeError,ValueError):choice_index=0
        try:position_id=int(payload.get('positionId',payload.get('positionid',0)) or 0)
        except (TypeError,ValueError):position_id=0
        picks=state.setdefault('picks',{})
        # The retail editor includes its current rearranged lineup on later
        # choices. Apply it before inserting the newly selected card, matching
        # the stable FIFA 20 flow.
        self._draft_apply_slot_order(state,payload.get('swapPlayerDefIds'))
        active=str(state['draftState'])
        choices=state.get('choices',{})
        if active=='PICK_DIFFICULTY':
            choice=choices['difficulty'][choice_index]
            state['difficulty']=int(choice['difficulty'])
            state['difficultyName']=choice['difficultyName']
            picks['difficulty']=self._draft_pick_row(choice,0)
            state['draftState']='FORMATION_DRAFT'
        elif active=='FORMATION_DRAFT':
            choice=choices['formation'][choice_index]
            state['formation']=choice['formation']
            picks['formation']=choice['formation']
            # Rebuild only the deterministic choice catalogue against the
            # selected layout before any captain/player has been chosen.
            state['choices']=self._draft_generate_choices(
                int(state.get('seed',0)),choice['formation'])
            choices=state['choices']
            state['draftState']='CAPTAIN_DRAFT'
        elif active=='CAPTAIN_DRAFT':
            if not choices['captain']:
                state['draftState']='PLAYER_DRAFT'
            else:
                choice=choices['captain'][choice_index]
                picks['captain']=self._draft_pick_row(choice,position_id,captain=True)
                state['captainSlot']=max(0,min(22,position_id))
                state['draftState']='PLAYER_DRAFT'
        elif active=='PLAYER_DRAFT':
            position_id=max(0,min(22,position_id))
            choice=choices.get(f'player:{position_id}') or []
            if not choice:
                choice=next((rows for key,rows in choices.items()
                             if key.startswith('player:') and rows),[])
            if choice:
                index=max(0,min(choice_index,len(choice)-1))
                picks.setdefault('players',{})[str(position_id)]= \
                    self._draft_pick_row(choice[index],position_id)
            filled={int(key) for key in picks.get('players',{})}
            captain_slot=state.get('captainSlot',-1)
            remaining=[slot for slot in range(23)
                       if slot not in filled and slot!=captain_slot]
            if not remaining:
                state['draftState']='MANAGER_DRAFT' if choices.get('manager') else 'READY_FOR_MATCH'
        elif active=='MANAGER_DRAFT':
            if choices.get('manager'):
                choice=choices['manager'][choice_index]
                picks['manager']=self._draft_pick_row(choice,0)
            state['draftState']='READY_FOR_MATCH'
        self._draft_save_state(state)
        self.event('draft_choose',stage=state['draftState'],
                   choiceIndex=choice_index,positionId=position_id)
        return self.draft_wire_state(self.draft_state_payload(state))

    def draft_autocomplete(self):
        state=self._draft_load_state()
        if not state or state.get('draftState') in ('NOT_STARTED','','COMPLETE',
                                                    'READY_FOR_REWARDS'):
            return self.draft_wire_state(self.draft_state_payload(state))
        rng=random.Random(int(state.get('seed',0))+7)
        picks=state.setdefault('picks',{})
        choices=state.get('choices',{})
        active=str(state['draftState'])
        if active in ('PICK_DIFFICULTY','FORMATION_DRAFT'):
            key='difficulty' if active=='PICK_DIFFICULTY' else 'formation'
            row=choices[key][rng.randrange(len(choices[key]))]
            if key=='difficulty':
                state['difficulty']=int(row['difficulty'])
                state['difficultyName']=row['difficultyName']
                picks['difficulty']=self._draft_pick_row(row,0)
            else:
                state['formation']=row['formation']
                picks['formation']=row['formation']
                state['choices']=self._draft_generate_choices(
                    int(state.get('seed',0)),row['formation'])
                choices=state['choices']
            state['draftState']='FORMATION_DRAFT' if key=='difficulty' else 'CAPTAIN_DRAFT'
        if state['draftState']=='CAPTAIN_DRAFT' and choices.get('captain'):
            position_id=6
            row=choices['captain'][rng.randrange(len(choices['captain']))]
            picks['captain']=self._draft_pick_row(row,position_id,captain=True)
            state['captainSlot']=position_id
            state['draftState']='PLAYER_DRAFT'
        if state['draftState']=='PLAYER_DRAFT':
            # JSON persists slot keys as strings. Comparing those keys with
            # integer slot numbers made Complete My Squad overwrite every
            # manually selected starter from CDM through the back line.
            filled={int(slot) for slot in picks.get('players',{})
                    if str(slot).isdigit()}
            for slot in range(23):
                if slot in filled or slot==state.get('captainSlot',-1):continue
                rows=choices.get(f'player:{slot}') or \
                     next((rows for key,rows in choices.items()
                           if key.startswith('player:') and rows),[])
                if not rows:continue
                row=rows[rng.randrange(len(rows))]
                picks.setdefault('players',{})[str(slot)]= \
                    self._draft_pick_row(row,slot)
            state['draftState']='MANAGER_DRAFT' if choices.get('manager') else 'READY_FOR_MATCH'
        if state['draftState']=='MANAGER_DRAFT':
            if choices.get('manager'):
                row=choices['manager'][rng.randrange(len(choices['manager']))]
                picks['manager']=self._draft_pick_row(row,0)
            state['draftState']='READY_FOR_MATCH'
        self._draft_save_state(state)
        self.event('draft_autocomplete',stage=state['draftState'])
        return self.draft_wire_state(self.draft_state_payload(state))

    def draft_autofill_response(self):
        """Native object-shaped operation result for Complete My Squad.

        Current-state reads are array-rooted, but autocomplete is a different
        command. Returning that state array here makes the global navigation
        handler consume READY_FOR_MATCH and crash while leaving the editor.
        """
        self.draft_autocomplete()
        state=self._draft_load_state()
        payload=self.draft_state_payload(state)
        wire=self.draft_wire_state(payload)
        squad=wire.get('squad') or self.draft_squad(state or {})
        return {'success':True,'mode':'SINGLE_PLAYER','draftState':wire,
                'squad':squad,'players':squad.get('players',[])}

    def draft_choose_difficulty(self,payload):
        payload=payload if isinstance(payload,dict) else {}
        state=self._draft_load_state()
        if not state or state.get('draftState')!='PICK_DIFFICULTY':
            return self.draft_wire_state(self.draft_state_payload(state))
        requested=str(payload.get('difficultyName',payload.get('difficulty',
                       payload.get('value',''))) or '').upper()
        aliases={'PROFESSIONAL':'PRO','WORLD_CLASS':'WORLDCLASS',
                 'SEMI_PRO':'SEMIPRO'}
        requested=aliases.get(requested,requested)
        match=next((value for value,name,_ in self.DRAFT_DIFFICULTIES
                    if name==requested),None)
        if match is None:
            try:match=max(1,min(6,int(payload.get('difficulty',payload.get('value',4)))))
            except (TypeError,ValueError):match=4
        picks=state.setdefault('picks',{})
        choice=next((row for row in state.get('choices',{}).get('difficulty',[])
                     if int(row.get('difficulty',0) or 0)==match),
                    {'difficulty':match,'difficultyName':requested})
        state['difficulty']=int(choice['difficulty'])
        state['difficultyName']=choice['difficultyName']
        picks['difficulty']=self._draft_pick_row(choice,0)
        state['draftState']='FORMATION_DRAFT'
        self._draft_save_state(state)
        self.event('draft_difficulty',difficulty=state['difficulty'])
        return self.draft_wire_state(self.draft_state_payload(state))

    def draft_opponent_squad(self,round_index):
        ratings=(80,83,86,89)
        chemistry=(82,88,94,100)
        clubs=((243,'Real Madrid','RMA'),(9,'Liverpool','LIV'),
               (21,'FC Bayern','FCB'),(241,'FC Barcelona','BAR'))
        round_index=max(0,min(3,int(round_index)))
        target=ratings[round_index]
        team_id,club_name,club_abbr=clubs[round_index]
        owned=set()
        for item in self.club.list_items():
            if item.get('itemType','player')=='player':
                owned.add(int(item.get('assetId') or item.get('resourceId') or 0))
        rows=self._sqbt_cpu_resource_ids(target,owned)
        if not rows:return None
        base_id=self.DRAFT_OPPONENT_SQUAD_ID*20+round_index*100
        players=[]
        for index,row in enumerate(rows):
            card=self._sqbt_catalogue_player_card(row,base_id+index)
            players.append({'index':index,'kitNumber':index+1 if index<11 else 0,
                            'loyaltyBonus':1,'itemData':card})
        rating=int(round(sum(int(r['rating']) for r in rows[:11])/11))
        home,away=self._sqbt_kit_items(team_id,40+round_index)
        badge=self.badge_item(team_id,base_id+82)
        badge.update(pile=7,itemState='activeBadge')
        ball=self.ball_item(base_id+83)
        actives=[ball,badge,home,away]
        return self._finish_squad(
            {'id':self.DRAFT_OPPONENT_SQUAD_ID+round_index,
             'squadId':self.DRAFT_OPPONENT_SQUAD_ID+round_index,'personaId':0,
             'name':club_name,'squadName':club_name,'clubName':club_name,
             'clubAbbr':club_abbr,'teamId':team_id,'clubId':team_id,
             'badgeId':team_id,'badgeAssetId':team_id,
             'formation':'f442','rating':rating,
             'starRating':rating,
             'squadType':'DREAM_SQUAD','dreamSquad':True,'active':True,'valid':True,
             'chemistry':chemistry[round_index],'players':players,
             'actives':actives,'manager':[self.cpu_manager(round_index,team_id)],
             'kicktakers':[],'club':actives,'newSquad':0,'newsquad':0})

    def draft_match_start(self,payload=None):
        payload=payload if isinstance(payload,dict) else {}
        state=self._draft_load_state()
        if not state or state.get('draftState')!='READY_FOR_MATCH':
            return None
        opponent=self.draft_opponent_squad(int(state.get('wins',0)))
        if opponent is None:return None
        now=int(time.time())
        opponent_id=int(opponent['squadId'])
        difficulty=int(state.get('difficulty',3) or 3)
        difficulty_name=next((name for value,name,_ in self.DRAFT_DIFFICULTIES
                              if value==difficulty),'PRO')
        state['activeMatch']={'matchStart':now,
                              'gid':int(payload.get('gid',payload.get('gameId',0)) or 0),
                              'opponentSquadId':opponent_id}
        self._draft_save_state(state)
        self.event('draft_match_start',round=state['wins']+1,
                   opponentSquadId=opponent_id)
        # Mirror the proven Squad Battles contract.  CreateMatch carries only
        # the user's selected XI; the CPU XI is exposed through its offline
        # squad endpoint. Embedding both squads made FIFA build two competing
        # match owners and stall on a black screen.
        user=self.draft_squad(state)
        actives=self.worn_actives();user['actives']=actives;user['club']=actives
        for squad in (user,opponent):
            squad.update(matchDifficulty=difficulty,difficulty=difficulty,
                         difficultyId=difficulty,gameDifficulty=difficulty,
                         sqbtMatchDifficulty=difficulty,
                         difficultyName=('PROFESSIONAL' if difficulty_name=='PRO'
                                         else difficulty_name))
        return {'startDateTime':now,'matchmakingElo':0,'mismatches':[],
                'champs2WinForm':0,'rivalsV2StageId':0,'squad':user,
                'opponentSquadId':opponent_id,
                'opponentTeamId':int(opponent.get('teamId',0)),
                'matchDifficulty':difficulty,'difficulty':difficulty,
                'difficultyId':difficulty,'gameDifficulty':difficulty,
                'sqbtMatchDifficulty':difficulty,
                'difficultyName':('PROFESSIONAL' if difficulty_name=='PRO'
                                  else difficulty_name)}

    def draft_match_end(self,payload):
        payload=payload if isinstance(payload,dict) else {}
        state=self._draft_load_state()
        if not state or state.get('draftState') not in ('READY_FOR_MATCH',
                                                        'READY_FOR_REWARDS'):
            return None
        # ProtoHttp may retry /match/end after the run settled. Replay the
        # stored receipt only for the same game id so a later squad-battles
        # report cannot be mistaken for a draft retry.
        receipt=state.get('lastMatchEndResponse')
        gid=int(payload.get('gid',payload.get('gameId',0)) or 0)
        if state.get('draftState')=='READY_FOR_REWARDS' and receipt:
            if gid and int(receipt.get('gid',0) or 0)==gid or not gid:
                response=dict(receipt)
                response['alreadySettled']=True
                return response
            return None
        if not state.get('activeMatch'):
            return None
        if state.get('draftState')!='READY_FOR_MATCH':
            return None
        state['activeMatch']=state.get('activeMatch') or {}
        active_match=dict(state['activeMatch'])
        match_start=int(active_match.get('matchStart',0) or 0)
        reason=str(payload.get('reason',payload.get('endReason',
                   payload.get('result',''))) or '').upper()
        report_items=payload.get('items') if isinstance(payload.get('items'),list) else []
        pre_match_cancel=(reason=='DNF' and gid==0
                          and int(payload.get('matchReportId',0) or 0)==0
                          and not report_items
                          and not str(payload.get('matchData','') or '')
                          and not payload.get('events'))
        if pre_match_cancel:
            state['activeMatch']=None
            state['lastMatchEndResponse']={}
            self._draft_save_state(state)
            balance=int(self.club.status()['credits'])
            response={'gid':0,'gameId':0,'matchReportId':0,'endReason':'DNF',
                      'secondsPlayed':0,'completionAward':0,'rewardCoins':0,
                      'totalCoins':0,'credits':balance,'coins':balance,
                      'draftWins':int(state.get('wins',0)),
                      'gamesWonCurrentMatch':int(state.get('wins',0)),
                      'squadState':'READY_FOR_MATCH','draftEliminated':False,
                      'won':0,'draw':0,'loss':0,
                      'currencies':[{'name':'coins','funds':balance,'finalFunds':balance},
                                    {'name':'points','funds':0,'finalFunds':0}]}
            self.event('draft_match_cancelled',wins=int(state.get('wins',0)))
            return response
        if reason in ('QUIT','DNF','FORFEIT','DISCONNECT'):reason='LOSS'
        if reason not in ('WIN','DRAW','LOSS'):
            # Deja the result undetermined so a valid retry can settle it.
            return {'endReason':'NO_CONTEST'}
        my_stats=payload.get('myMatchStats') if isinstance(payload.get('myMatchStats'),dict) else {}
        opponent_stats=(payload.get('opponentMatchStats')
                        if isinstance(payload.get('opponentMatchStats'),dict) else {})
        goals=int(payload.get('goals',payload.get('score',
                  my_stats.get('goals',my_stats.get('goalsWithMultipliers',0)))) or 0)
        goals_against=int(payload.get('goalsAgainst',payload.get('oppScore',
                          payload.get('opponentScore',opponent_stats.get('goals',
                          opponent_stats.get('goalsWithMultipliers',0))))) or 0)
        wins=int(state.get('wins',0))
        mult=next((m for value,_,m in self.DRAFT_DIFFICULTIES
                   if value==int(state.get('difficulty',0) or 0)),1.0)
        coins=(int(max(150,325+goals*40-goals_against*10)*mult)
               if reason!='LOSS' else max(150,int(150*mult)))
        if reason=='WIN':wins+=1
        state.setdefault('matches',[]).append({
            'round':int(state.get('round',1)),'result':reason,
            'score':goals,'opponentScore':goals_against,
            'penaltyScore':0,'opponentPenaltyScore':0})
        state['wins']=wins
        state['round']=wins+1
        if reason=='WIN':state['matchesPlayed']=int(state.get('matchesPlayed',0))+1
        else:state['losses']=int(state.get('losses',0))+1
        if reason!='WIN' or wins>=4:
            state['draftState']='READY_FOR_REWARDS'
            state['completed']=True
        state['activeMatch']=None
        balance=int(self.club.status()['credits'])+coins
        self.club.set_meta('credits',balance)
        response={'endReason':reason,'draftWins':wins,'gamesWonCurrentMatch':wins,
                  'completionAward':coins,'rewardCoins':coins,'totalCoins':coins,
                  'score':goals,'myScore':goals,'goals':goals,
                  'opponentScore':goals_against,'goalsAgainst':goals_against,
                  'won':1 if reason=='WIN' else 0,
                  'draw':1 if reason=='DRAW' else 0,
                  'loss':1 if reason=='LOSS' else 0,
                  'coins':balance,'credits':balance,
                  'currencies':[{'name':'coins',
                                 'funds':balance,'finalFunds':balance},
                                {'name':'points','funds':0,'finalFunds':0}],
                  'draftEliminated':state['draftState']=='READY_FOR_REWARDS',
                  'alreadySettled':False,'gid':gid}
        objective_identity=gid or int(active_match.get('gid',0) or 0) or match_start
        objective_key=(f'draft:{objective_identity}:'
                       f'{int(state.get("round",wins+1) or wins+1)}:{match_start}')
        self.record_objective_match('draft',reason,objective_key)
        state['lastMatchEndResponse']=response
        self._draft_save_state(state)
        self.event('draft_match_end',reason=reason,wins=wins,score=goals,
                   opponentScore=goals_against,coins=coins,balance=balance,
                   stage=state['draftState'])
        return response

    def draft_award_preview(self,state):
        wins=int(state.get('wins',0))
        seed=int(state.get('seed',1) or 1)
        bundles=self.DRAFT_REWARD_PACKS[min(4,wins)]
        packs=bundles[random.Random(
            f'fut22-draft-reward:{wins}:{seed}').randrange(len(bundles))]
        # FutDraftRewardItem: 2=coins, 0=pack.  Keep the three native fields;
        # extra invented members make the retail rewards parser discard rows.
        return ([{'halId':0,'type':2,
                  'value':self.DRAFT_REWARD_COINS[min(4,wins)]}]+
                [{'halId':int(pack_id),'type':0,'value':1}
                 for pack_id in packs])

    def draft_claim_award(self):
        state=self._draft_load_state()
        if not state or state.get('draftState')!='READY_FOR_REWARDS':
            return None
        wins=int(state.get('wins',0))
        coins=self.DRAFT_REWARD_COINS[min(4,wins)]
        awards=self.draft_award_preview(state)
        player_picks=[]
        credits=int(self.club.status()['credits'])+coins
        self.club.set_meta('credits',credits)
        pack_ids=[int(row['halId']) for row in awards
                  if int(row.get('type',-1))==0 and int(row.get('halId',0))>0]
        for pack_id in pack_ids:
            self.club.grant_reward_pack(
                pack_id,source=f'draft:{int(state.get("draftId",0))}:wins:{wins}')
        # Clear the run so the next draft/state read shows the entry screen.
        self._draft_save_state({'draftState':'NOT_STARTED'})
        self.event('draft_award',wins=wins,coins=coins,
                   packs=pack_ids,playerGranted=bool(player_picks))
        return awards,player_picks,credits

    def draft_abandon(self):
        state=self._draft_load_state()
        if not state or state.get('draftState') in ('NOT_STARTED','','COMPLETE'):
            return False
        # Refund the entry fee: the run never reached a reward.
        credits=int(self.club.status()['credits'])+self.DRAFT_ENTRY_FEE
        self.club.set_meta('credits',credits)
        self._draft_save_state({'draftState':'NOT_STARTED'})
        self.event('draft_abandon',refunded=self.DRAFT_ENTRY_FEE)
        return True

    def draft_stats(self):
        state=self._draft_load_state() or {}
        wins=int(state.get('wins',0))
        losses=int(state.get('losses',0))
        completed=1 if state.get('completed') else 0
        return {'bestBuilderScore':0,'concededGoals':0,
                'draftChampion':1 if completed and wins>=4 else 0,
                'draftsCompleted':completed,'gamesLost':losses,'gamesWon':wins,
                'passAccuracyTotal':0,'possessionPercentage':0,
                'possesionTotal':0,'possessionTotal':0,'scoredGoals':0}

    def credits_payload(self):
        # RS4:FutUserCreditsServerResponse reads exactly two keys: currencies
        # and unopenedPacks (fifa22work parser notes at 0x1802EBD5A).
        value=int(self.club.status()['credits'])
        return {'currencies':[{'name':'COINS','funds':value,'finalFunds':value},
                              {'name':'POINTS','funds':0,'finalFunds':0},
                              {'name':'DRAFT_TOKEN','funds':0,'finalFunds':0}],
                'unopenedPacks':self.unopened_packs_payload()}

    def unopened_packs_payload(self):
        rows=self.club.reward_packs()
        packs=[{'id':int(row['id']),'rewardInstanceId':int(row['id']),
                'packId':int(row['pack_id']),'source':row['source']}
               for row in rows]
        return {'preOrderPacks':0,'recoveredPacks':len(packs),
                'count':len(packs),'total':len(packs),'packs':packs,
                'pack':packs}

    @staticmethod
    def _sbc_rule_wire(rule,slot):
        keys={'chemistry':('TEAM_CHEMISTRY',1),'player_count':('PLAYER_COUNT',2),
              'quality':('PLAYER_QUALITY',3),'same_nation':('SAME_NATION_COUNT',4),
              'same_league':('SAME_LEAGUE_COUNT',5),'same_club':('SAME_CLUB_COUNT',6),
              'nation_count':('NATION_COUNT',7),'league_count':('LEAGUE_COUNT',8),
              'club_count':('CLUB_COUNT',9),'rare_count':('PLAYER_RARITY',18),
              'team_rating':('TEAM_RATING',19)}
        kind=str(rule.get('kind',''));scope={'min':0,'max':1,'exact':2}.get(
            str(rule.get('scope','min')),0)
        rule_type,key=keys.get(kind,(kind.upper(),0))
        value=int(rule.get('value',0) or 0);count=int(rule.get('count',-1) or -1)
        if kind=='quality' and count>=0:
            return [{'type':'PLAYER_COUNT','eligibilitySlot':slot,'eligibilityKey':2,
                     'eligibilityValue':count},
                    {'type':'SCOPE','eligibilitySlot':slot,'eligibilityKey':13,
                     'eligibilityValue':scope},
                    {'type':'PLAYER_LEVEL','eligibilitySlot':slot,'eligibilityKey':17,
                     'eligibilityValue':value}]
        if kind=='rare_count':
            return [{'type':'PLAYER_COUNT','eligibilitySlot':slot,'eligibilityKey':2,
                     'eligibilityValue':value},
                    {'type':'SCOPE','eligibilitySlot':slot,'eligibilityKey':13,
                     'eligibilityValue':scope},
                    {'type':'PLAYER_RARITY','eligibilitySlot':slot,'eligibilityKey':18,
                     'eligibilityValue':1}]
        return [{'type':rule_type,'eligibilitySlot':slot,'eligibilityKey':key,
                 'eligibilityValue':value},
                {'type':'SCOPE','eligibilitySlot':slot,'eligibilityKey':13,
                 'eligibilityValue':scope}]

    @staticmethod
    def _sbc_awards(awards):
        configs={int(row['id']):row for row in STORE_PACKS}
        result=[]
        for raw in awards:
            count=max(1,int(raw.get('count',1) or 1));kind=str(raw.get('type','')).upper()
            if kind=='PACK':
                pack_id=int(raw.get('packId',raw.get('pack_id',0)) or 0)
                cfg=configs.get(pack_id,{})
                result.append({'value':pack_id,'awardValue':pack_id,'type':'pack',
                               'awardType':'pack','count':count,'awardCount':count,
                               'halId':int(cfg.get('packAssetId',3)),'packId':pack_id,
                               'packAssetId':int(cfg.get('packAssetId',3)),
                               'name':cfg.get('name',f'FUT Pack {pack_id}'),
                               'displayName':cfg.get('name',f'FUT Pack {pack_id}'),
                               'isUntradeable':True,'untradeable':True,'loan':0})
            elif kind=='COINS':
                value=max(0,int(raw.get('value',0) or 0))*count
                result.append({'value':value,'awardValue':value,'type':'coin',
                               'awardType':'coin','count':1,'awardCount':1,
                               'halId':0,'isUntradeable':False,'loan':0})
        return result

    def sbc_sets_payload(self):
        challenge_counts,set_counts=self.club.sbc_completion_counts()
        categories=[]
        for priority,(category_id,name) in enumerate(
                ((1,'BASIC'),(2,'ADVANCED'),(3,'UPGRADES')),1):
            sets=[]
            for entry in SBC_CATALOG:
                if int(entry['categoryId'])!=category_id:continue
                completed=sum(1 for row in entry['challenges']
                              if challenge_counts.get(int(row['challengeId']),0))
                repeatable=bool(entry.get('repeatable',False))
                visible_completed=0 if repeatable else completed
                sets.append({'setId':int(entry['setId']),'name':entry['name'],
                             'description':entry['description'],
                             'priority':int(entry['priority']),'categoryId':category_id,
                             'challengesCount':len(entry['challenges']),
                             'challengesCompletedCount':visible_completed,'hidden':False,
                             'tagged':0,'endTime':0,'repeatable':repeatable,
                             'tutorial':False,'timesCompleted':set_counts.get(int(entry['setId']),0),
                             'taggedByProduction':False,'taggedByUser':False,
                             'setImageId':1000000+int(entry['setId']),
                             'awards':self._sbc_awards(list(entry.get('awards',[])))})
            categories.append({'categoryId':category_id,'id':category_id,
                               'name':name,'priority':priority,'sets':sets})
        return {'categories':categories}

    def sbc_challenge_payload(self,set_entry,challenge,priority=0):
        counts,_=self.club.sbc_completion_counts()
        requirements=[]
        rules=[row for row in challenge.get('rules',[])
               if row.get('kind')!='player_count']
        for slot,rule in enumerate(rules,1):
            requirements.extend(self._sbc_rule_wire(rule,slot))
        challenge_id=int(challenge['challengeId'])
        requirements=[{'eligibilitySlot':int(row['eligibilitySlot']),
                       'eligibilityKey':int(row['eligibilityKey']),
                       'eligibilityValue':int(row['eligibilityValue'])}
                      for row in requirements]
        repeatable=bool(set_entry.get('repeatable',False))
        preview_awards=list(challenge.get('awards',[]))
        if not preview_awards and len(set_entry.get('challenges',[]))==1:
            preview_awards=list(set_entry.get('awards',[]))
        return {'name':challenge['name'],'priority':priority,
                'status':0 if repeatable else (1 if counts.get(challenge_id) else 0),
                'setId':int(set_entry['setId']),'description':challenge['description'],
                'challengeId':challenge_id,'endTime':0,'repeatable':repeatable,
                'formation':str(challenge.get('formation','f442')).lstrip('f'),
                'timesCompleted':int(counts.get(challenge_id,0) or 0),
                'elgReq':requirements,'elgOperation':'AND',
                'tutorial':False,'type':0,
                'squadId':0,'categoryId':int(set_entry['categoryId']),
                'challengeImageId':100000+challenge_id,
                'awards':self._sbc_awards(preview_awards)}

    @staticmethod
    def _sbc_empty_item():
        return {'id':0,'assetId':0,'resourceId':0,'rating':0,
                'itemType':'player','itemState':'invalid','resourceGameYear':2022,
                'owners':0,'discardValue':0,'cardsubtypeid':0,'rareflag':0,
                'preferredPosition':'any','attributeList':[],'statsList':[],
                'lifetimeStats':[],'teamid':0,'nation':0,'pile':0}

    def sbc_squad_response(self,set_entry,challenge):
        saved=self.club.sbc_squad(int(challenge['challengeId'])) or {}
        by_id={int(row['id']):row for row in self.club.list_items('club')}
        saved_slots={index:item_id for index,item_id in
                     self.club._sbc_entries(saved,23)}
        players=[];ratings=[]
        for index in range(23):
            item=by_id.get(saved_slots.get(index,0))
            if item:
                wire=self.item_wire(item);ratings.append(int(item.get('rating',0) or 0))
            else:wire=self._sbc_empty_item()
            players.append({'index':index,'itemData':wire})
        size=int(challenge['players']);chem=int(saved.get('chemistry',0) or 0)
        return {'challengeId':int(challenge['challengeId']),
                'playerRequirements':[{'index':i,
                                       'playerType':'DEFAULT' if i<size else 'BRICK'}
                                      for i in range(11)],
                'squad':{'id':1,'formation':challenge.get('formation','f442'),
                         'rating':int(round(sum(ratings)/len(ratings))) if ratings else 0,
                         'chemistry':max(0,min(100,chem)),'manager':[],
                         'players':players}}

    def sbc_evaluate(self,challenge,saved):
        ids=self.club._sbc_item_ids(saved or {})
        by_id={int(row['id']):row for row in self.club.list_items('club')}
        items=[by_id[item_id] for item_id in ids
               if item_id in by_id and by_id[item_id].get('itemType','player')=='player']
        ratings=[int(row.get('rating',0) or 0) for row in items]
        qualities=[3 if value>=75 else 2 if value>=65 else 1 for value in ratings]
        nations=[int(row.get('nation',row.get('nationId',0)) or 0) for row in items]
        leagues=[int(row.get('leagueId',0) or 0) for row in items]
        clubs=[int(row.get('teamid',0) or 0) for row in items]
        def distinct(values):return len({value for value in values if value>0})
        def largest(values):
            return max((values.count(value) for value in set(values) if value>0),default=0)
        chemistry=int((saved or {}).get('chemistry',-1))
        if chemistry<0:chemistry=100
        actuals={'player_count':len(items),'quality':qualities,
                 'rare_count':sum(int(row.get('rareflag',0) or 0)>0 for row in items),
                 'nation_count':distinct(nations),'league_count':distinct(leagues),
                 'club_count':distinct(clubs),'same_nation':largest(nations),
                 'same_league':largest(leagues),'same_club':largest(clubs),
                 'team_rating':int(round(sum(ratings)/len(ratings))) if ratings else 0,
                 'chemistry':chemistry}
        checks=[];valid_all=True
        for rule in challenge.get('rules',[]):
            kind=str(rule['kind']);target=int(rule.get('value',0));scope=str(rule.get('scope','min'))
            count=int(rule.get('count',-1));actual=actuals.get(kind,0)
            if kind=='quality':
                if count>=0:
                    value=sum(1 for q in actual if q>=target) if scope=='min' else sum(1 for q in actual if q==target)
                    valid=value>=count;shown=value;wanted=count
                else:
                    valid=bool(actual) and all((q==target if scope=='exact' else q>=target if scope=='min' else q<=target) for q in actual)
                    shown=min(actual,default=0);wanted=target
            else:
                shown=int(actual);wanted=target
                valid=shown>=target if scope=='min' else shown<=target if scope=='max' else shown==target
            checks.append({'type':kind,'description':rule.get('text',kind),
                           'actual':shown,'target':wanted,'scope':scope,'valid':bool(valid)})
            valid_all=valid_all and bool(valid)
        return valid_all,checks,ids

    # --- Store: the pack list the store screen actually renders ---------
    # Field set and gotchas follow the fifa22work reverse-engineering of
    # RS4:FutStoreGetPackTypesServerResponse: 'state':'active' keeps a pack
    # out of the availability filter, 'useDefaultImage':False opens the tile
    # image, actionType 'CREATEPACK' is the only post-purchase action the
    # client acts on, and quantities live inside packContentInfo.

    _STORE_CATEGORIES=(
        {'categoryId':1,'localizedName':'Bronze','order':1},
        {'categoryId':2,'localizedName':'Silver','order':2},
        {'categoryId':3,'localizedName':'Gold','order':3},
        {'categoryId':4,'localizedName':'Promo','order':4},
        {'categoryId':5,'localizedName':'My Packs','order':5},
    )
    _ODDS_BANDS=(('bronze',0,65,1,0),('silver',65,75,1,1),('gold',75,80,1,2),
                 ('elite',80,85,1,2),('icon',85,200,3,0))

    def store_categories(self):
        return {'storeCategoryDtos':[dict(row) for row in self._STORE_CATEGORIES]}

    def _store_band_counts(self,min_rating,max_rating=99):
        """Catalogue player count and per-band shares for pack odds."""
        histogram=getattr(self,'_store_rating_histogram',None)
        if histogram is None:
            catalogue=getattr(self,'_catalogue_cache',None)
            if catalogue is None:
                catalogue=load_catalogue()
                self._catalogue_cache=catalogue
            histogram=[0]*100
            for row in catalogue['players']:
                rating=max(0,min(99,int(row['rating'])))
                histogram[rating]+=1
            self._store_rating_histogram=histogram
        bands={name:0 for name,_,_,_,_ in self._ODDS_BANDS}
        total=0
        for rating in range(max(0,int(min_rating)),min(99,int(max_rating))+1):
            count=histogram[rating]
            if not count:continue
            total+=count
            for name,lo,hi,_,_ in self._ODDS_BANDS:
                if lo<=rating<hi:
                    bands[name]+=count
                    break
        return bands,total

    def _store_pack_row(self,pack,order):
        price=int(pack['cost'])
        quantity=int(pack['quantity'])
        min_rating=int(pack['minRating'])
        points=max(0,price//10)
        bands,total=self._store_band_counts(min_rating,int(pack.get('maxRating',99)))
        content={'itemQuantity':quantity,'goldQuantity':0,'silverQuantity':0,
                 'bronzeQuantity':0,'rareQuantity':int(pack['minRare']),
                 'contentType':'players'}
        odds=[]
        for index,(name,lo,hi,rid,qid) in enumerate(self._ODDS_BANDS):
            count=bands.get(name,0)
            if count and total:
                chance=min(100.0,count/total*100*max(1,quantity))
                value=min(100.0,chance)
                text_value=(f'{int(value)}%' if value.is_integer()
                            else f'{value:.1f}%')
                expected=quantity*count/max(1,total)
            else:
                text_value='0%';expected=0
            odds.append({'packId':pack['id'],'categoryId':index+1,'order':index,
                         'description':name.capitalize(),
                         'oddsFormatted':text_value,
                         'playerCount':max(0,int(round(expected))),
                         'qualityId':0 if rid>1 else qid,
                         'rarityAssetId':rid,'startTimeSec':0})
        if pack.get('contentType') not in (None,'players'):
            content['contentType']=str(pack.get('contentType'))
        elif pack.get('group')=='bronze':
            content['bronzeQuantity']=quantity
        elif pack.get('group')=='silver':
            content['silverQuantity']=quantity
        elif min_rating>=65:
            content['bronzeQuantity']=0
            content['silverQuantity']=0 if min_rating>=75 else quantity
            content['goldQuantity']=quantity if min_rating>=75 else 0
        elif min_rating>=1:
            content['bronzeQuantity']=quantity
        return {
            'id':pack['id'],'assetId':pack['packAssetId'],
            'state':'active','useDefaultImage':False,
            'actionType':'CREATEPACK','packType':'cardpack',
            'nameLoc':f'FUT_STORE_PACK_{pack["id"]}_NAME',
            'descriptionLoc':f'FUT_STORE_PACK_{pack["id"]}_DESC',
            'description':pack['description'],
            'sortPriority':order,'saleType':'NONE',
            'isPremium':price>=5000,
            'isPreviewable':(not pack.get('skipAnimation')
                             and pack.get('contentType') in (None,'players')),
            'skipPackOpeningAnimation':bool(pack.get('skipAnimation')),
            'packOpeningAnimationEnabled':not bool(pack.get('skipAnimation')),
            'currencies':[{'name':'coins','funds':price,'finalFunds':price},
                          {'name':'points','funds':points,'finalFunds':points}],
            'packContentInfo':content,
            'packOdds':odds,
            'categoryList':[{'categoryId':pack['categoryId']}],
        }

    def _store_roll(self,pack):
        """Draw one pack's contents without charging; the pool rules match
        Club.purchase so the preview shows what a purchase would deliver."""
        owned=getattr(self,'_store_roll_owned',None)
        if owned is None:
            owned={int(row.get('resourceId',0) or 0)
                   for row in self.club.list_items()
                   if str(row.get('itemType','player')).lower() in
                      ('player','kit','custom','ball','stadium','manager')}
        item_pool=list(pack.get('itemPool') or [])
        if item_pool:
            rng=random.SystemRandom()
            consumables=(str(pack.get('contentType','')).lower() in
                         ('consumables','position_modifiers'))
            if consumables:
                return [dict(rng.choice(item_pool))
                        for _ in range(int(pack['quantity']))]
            if str(pack.get('contentType','')).lower()=='club_items':
                if len(item_pool)<int(pack['quantity']):return []
                return [dict(row) for row in
                        rng.sample(item_pool,int(pack['quantity']))]
            available=list(item_pool)
            if len(available)<int(pack['quantity']):return []
            return [dict(row) for row in rng.sample(available,int(pack['quantity']))]
        catalogue=getattr(self,'_catalogue_cache',None)
        if catalogue is None:
            catalogue=load_catalogue()
            self._catalogue_cache=catalogue
        pool=[row for row in catalogue['players']
              if int(row['rating'])>=int(pack['minRating'])]
        max_rating=int(pack.get('maxRating',99) or 99)
        pool=[row for row in pool if int(row['rating'])<=max_rating]
        promo_only=pack.get('pool')=='special'
        if promo_only:
            pool=[row for row in pool if row.get('special')]
        elif pack.get('pool')=='base_common':
            pool=[row for row in pool if not row.get('special')
                  and int(row.get('rareflag',0) or 0)==0]
        elif pack.get('group')!='gold':
            pool=[row for row in pool if not row.get('special')]
        base=[row for row in pool if not row.get('special')]
        specials=[row for row in pool if row.get('special')]
        rare=[row for row in base if int(row['rareflag'])>0]
        if not pool or (int(pack['minRare']) and not rare and not promo_only):return []
        rng=random.SystemRandom()
        result=[];used=set();family_counts={}
        def promo_card():
            by_family={}
            for row in specials:
                if int(row['resourceId']) in used:continue
                family=str(row.get('specialVersion') or row.get('promo') or
                           row.get('rareflag') or 'special')
                by_family.setdefault(family,[]).append(row)
            if not by_family:return None
            minimum=min(family_counts.get(family,0) for family in by_family)
            families=[family for family in by_family
                      if family_counts.get(family,0)==minimum]
            family=rng.choice(families)
            family_counts[family]=family_counts.get(family,0)+1
            return rng.choice(by_family[family])
        for index in range(int(pack['quantity'])):
            roll_promo=(promo_only or (specials and pack.get('group')=='gold'
                        and rng.random()<float(pack.get('specialChance',0))))
            row=promo_card() if roll_promo else None
            if row is None:
                source=rare if index<int(pack['minRare']) else base
                candidates=[candidate for candidate in source
                            if int(candidate['resourceId']) not in used]
                if not candidates:
                    row=promo_card()
                    if row is None:return []
                else:row=rng.choice(candidates)
            result.append(row)
            used.add(int(row['resourceId']))
        return result

    def preview_roll(self,pack_id,refresh=True):
        """Roll/remember a preview; non-refresh reads replay the same envelope."""
        rolls=getattr(self,'_preview_rolls',None)
        if rolls is None:
            rolls={};self._preview_rolls=rolls
        pack=next((p for p in STORE_PACKS if p['id']==int(pack_id)),None)
        if pack is None:return None
        if not refresh and int(pack_id) in rolls:
            if pack.get('itemPool'):
                by_resource={int(row['resourceId']):row
                             for row in pack['itemPool']}
                return [dict(by_resource[int(resource_id)]) for resource_id in
                        rolls[int(pack_id)] if int(resource_id) in by_resource]
            catalogue=getattr(self,'_catalogue_cache',None)
            if catalogue is None:
                catalogue=load_catalogue();self._catalogue_cache=catalogue
            by_resource={int(row['resourceId']):row
                         for row in catalogue['players']}
            remembered=[by_resource.get(int(resource_id))
                        for resource_id in rolls[int(pack_id)]]
            return [row for row in remembered if row is not None]
        roll=self._store_roll(pack)
        rolls[int(pack_id)]=[row['resourceId'] for row in roll]
        operations=getattr(self,'_preview_operation_ids',None)
        if operations is None:
            operations={};self._preview_operation_ids=operations
        operations[int(pack_id)]=f'preview:{int(pack_id)}:{secrets.token_hex(12)}'
        return roll

    @staticmethod
    def _pack_reveal_key(item):
        return (0 if (bool(item.get('special')) or
                      int(item.get('rareflag',0) or 0)>1) else 1,
                -int(item.get('rating',0) or 0),
                -int(item.get('rareflag',0) or 0),
                -int(item.get('resourceId',0) or 0))

    def _store_preview_item(self,pack,order):
        """Publish the bought pack's real headline card before purchase.

        FIFA's no-preview walkout hook captures this parsed store item and the
        purchase selects its capture slot through walkout-request.ini. This is
        the measured FIFA 22 path that keeps the retail Coins/Points popup.
        """
        if pack.get('skipAnimation'):return None
        # Store refreshes replay the same preview. Re-rolling every list call
        # forced dozens of new action-shot textures into FIFA's cache and was
        # the direct trigger for the intermittent Updating Store loop.
        roll=self.preview_roll(int(pack['id']),refresh=False) or []
        players=[row for row in roll
                 if str(row.get('itemType','player')).lower()=='player']
        if not players:return None
        head=sorted(players,key=self._pack_reveal_key)[0]
        item_id=9_500_000+int(order)
        wire=self._sqbt_catalogue_player_card(head,item_id)
        # previewItem is parsed only so the native ceremony shim can retain an
        # exact FUT item object. It is not visible on the Store tile. Do not
        # attach the dynamic-image selector here: the live trace proved FIFA
        # synchronously fetched a batch of action shots under Updating Store
        # and never completed the transition. The purchased/new-item copy of
        # the card still carries its normal dynamic art.
        wire.pop('guidAssetId',None)
        self._preview_item_index[int(pack['id'])]=len(self._preview_item_index)
        return wire

    def publish_walkout_request(self,pack_id,items):
        players=[item for item in items
                 if str(item.get('itemType','player')).lower()=='player']
        if not players:return
        head=sorted(players,key=self._pack_reveal_key)[0]
        self._walkout_sequence=(self._walkout_sequence+1)&0x7fffffff
        text_value=(
            '[Walkout]\n'
            f'Seq={self._walkout_sequence}\n'
            f'Overall={int(head.get("rating",0) or 0)}\n'
            f'Team={int(head.get("teamid",head.get("teamId",0)) or 0)}\n'
            f'Nation={int(head.get("nation",head.get("nationId",0)) or 0)}\n'
            f'Rarity={int(head.get("rareflag",0) or 0)}\n'
            f'Discard={int(head.get("discardValue",0) or 0)}\n'
            f'AssetId={int(head.get("assetId",0) or 0)}\n'
            f'ResourceId={int(head.get("resourceId",0) or 0)}\n'
            f'ItemId={int(head.get("id",0) or 0)}\n'
            f'Expected={1 if int(head.get("rating",0) or 0)>=86 else 0}\n'
            # purchased/items is sorted with the reveal headline first. The
            # DLL now captures that actual purchase array instead of retaining
            # hidden Store-tile preview objects across menu transitions.
            'HeadlineIndex=0\n')
        try:
            self.walkout_request.write_text(text_value,encoding='utf-8')
            self.event('walkout_request',packId=int(pack_id),
                       resourceId=int(head.get('resourceId',0) or 0),
                       rating=int(head.get('rating',0) or 0),
                       headlineIndex=0)
        except OSError as exc:
            self.event('walkout_request_failed',packId=int(pack_id),error=str(exc))

    def clear_walkout_request(self,reason):
        # The native ceremony probe reads this file on every configure call.
        # A previous animated pack's HeadlineIndex must not be reused for a
        # player pick (or another pack whose opening animation is skipped).
        try:
            self.walkout_request.write_text('[Walkout]\nHeadlineIndex=-1\n',encoding='utf-8')
            self.event('walkout_request_cleared',reason=reason)
        except OSError as exc:
            self.event('walkout_request_clear_failed',reason=reason,error=str(exc))

    def store_pack_list(self):
        """Return only fields consumed by FutStoreGetPackTypesServerResponse.

        Do not attach hidden previewItem player objects. FIFA builds the
        ceremony headline from the purchase response; retaining one player
        object for every Store tile kept stale card-art pointers alive across
        Draft/match transitions and caused both Updating Store loops and the
        later gold/dynamic portrait corruption.
        """
        self._preview_item_index={}
        self._store_roll_owned={int(row.get('resourceId',0) or 0)
                                for row in self.club.list_items()
                                if str(row.get('itemType','player')).lower() in
                                   ('player','kit','custom','ball','stadium','manager')}
        packs=[]
        try:
            rewards_by_pack={}
            for reward in self.club.reward_packs():
                rewards_by_pack.setdefault(int(reward['pack_id']),[]).append(reward)
            for order,pack in enumerate(STORE_PACKS,start=1):
                reward_rows=rewards_by_pack.get(int(pack['id']),[])
                if pack.get('rewardOnly') and not reward_rows:
                    continue
                row=self._store_pack_row(pack,order)
                if reward_rows:
                    # A reward pack must replace its paid catalogue row.  If
                    # both rows share the same id, FIFA 22 renders My Packs but
                    # cannot resolve a native action when Select is pressed.
                    # These aliases mirror the retail/FIFA 20 My Packs
                    # contract and make the pack open directly (no currency
                    # chooser) through the existing purchased/items route.
                    quantity=len(reward_rows)
                    row.update({
                        'packId':int(pack['id']),
                        'packAssetId':int(pack['packAssetId']),
                        'quantity':quantity,
                        'unopenedQuantity':quantity,
                        'unopened':True,
                        'isReward':True,
                        'isMyPack':True,
                        'tradable':False,
                        'tradeable':False,
                        'name':str(pack['name']),
                        'packName':str(pack['name']),
                        'displayName':str(pack['name']),
                        'title':str(pack['name']),
                        'finalPrice':0,
                        'originalPrice':0,
                        'coins':0,
                        'points':0,
                        'currencies':[
                            {'name':'coins','funds':0,'finalFunds':0},
                            {'name':'points','funds':0,'finalFunds':0},
                        ],
                        'displayGroup':{'value':'mypacks','priority':0},
                        'purchaseGroup':'mypacks',
                        'group':'mypacks',
                        'visible':True,
                        'isPurchaseable':False,
                        'isCoinPurchasable':False,
                        'isFifaPointPurchasable':False,
                        'isPromo':False,
                        'isAvailableInStore':False,
                        'isPreviewable':False,
                        'categoryList':[{'categoryId':5,'orderInCategory':order}],
                        'rewardInstanceId':int(reward_rows[0]['id']),
                        'rewardInstanceIds':[int(value['id'])
                                             for value in reward_rows],
                    })
                packs.append(row)
        finally:
            del self._store_roll_owned
        return {'purchase':packs,
                # False keeps the retail Coins/Points purchase popup.  Per-pack
                # isPreviewable remains the ceremony switch after purchase.
                'previewEnabled':False,
                'packOddsAvailabilityState':'AVAILABLE',
                'timestamp':int(time.time())}

    def _player_pick_resource_ids(self,count=3,min_rating=84):
        catalogue=getattr(self,'_catalogue_cache',None)
        if catalogue is None:
            catalogue=load_catalogue();self._catalogue_cache=catalogue
        excluded={'sbc_base','sbc_premium','sbc_flashback','fut_showdown',
                  'fut_showdown_boost','objectives_reward','player_moments'}
        pool=[row for row in catalogue['players']
              if row.get('special') and int(row.get('rating') or 0)>=int(min_rating)
              and int(row.get('leagueId',0) or 0)!=2136
              and row.get('specialVersion') not in excluded]
        count=max(2,min(5,int(count)))
        if len(pool)<count:raise StateError('Not enough promo player-pick cards')
        # Never show two editions of the same footballer in one choice.
        by_asset={}
        for row in pool:by_asset.setdefault(int(row['assetId']),[]).append(row)
        if len(by_asset)<count:raise StateError('Not enough distinct player-pick cards')
        assets=random.SystemRandom().sample(list(by_asset),count)
        return [int(random.SystemRandom().choice(by_asset[asset])['resourceId'])
                for asset in assets]

    def grant_player_pick(self,count=3,min_rating=84,operation_id=None,title=None):
        count=max(2,min(5,int(count)))
        return self.club.grant_player_pick(
            self._player_pick_resource_ids(count,min_rating),
            operation_id or f'pick:{secrets.token_hex(12)}',
            title or f'1 of {count} {int(min_rating)}+ Promo Players')

    def purchase_player_pick(self,pack,operation_id):
        count=max(2,min(5,int(pack.get('playerPick',3) or 3)))
        minimum=int(pack.get('pickMinRating',84) or 84)
        title=f'1 of {count} {minimum}+ Promo Players'
        return self.club.purchase_player_pick(
            int(pack['id']),self._player_pick_resource_ids(count,minimum),
            operation_id,title)

    def player_pick_items(self):
        pending=self.club.pending_player_picks()
        if not pending:return []
        pick=next((entry for entry in pending if entry.get('opened')),pending[0])
        definitions=self.club.definitions([int(resource_id)
                                           for resource_id in pick['choices']])
        owned={int(row.get('resourceId',0)):int(row['id'])
               for row in self.club.list_items('club')
               if row.get('itemType','player')=='player'}
        cards=[]
        for index,resource_id in enumerate(pick['choices']):
            row=definitions.get(int(resource_id))
            if row is None:continue
            item_id=9_500_000+int(pick['id'])*10+index
            card=self.player_item_wire(dict(row,id=item_id,
                                            untradeable=True,tradeable=False))
            for key in ('pile','definitionId','_definitionId'):
                card.pop(key,None)
            duplicate_id=owned.get(int(resource_id),0)
            card.update(duplicate=bool(duplicate_id),
                        isDuplicate=bool(duplicate_id),
                        owned=bool(duplicate_id),
                        duplicateItemId=int(duplicate_id))
            cards.append(card)
        return sorted(cards,key=lambda card:int(card.get('rating') or 0),reverse=True)

    def player_pick_payload(self):
        pending=self.club.pending_player_picks()
        if not pending:
            return {'success':True,'playerPicks':[],'picks':[],'items':[],
                    'itemData':[],'itemList':[],'numberItems':0,
                    'availablePicks':0,'pickSize':0,'selectionSize':0,'count':0}
        pick=next((entry for entry in pending if entry.get('opened')),pending[0])
        items=self.player_pick_items();token_id=self.club.player_pick_placeholder_id(pick['id'])
        ownership=[bool(item.get('isDuplicate')) for item in items]
        duplicates=[{'itemId':int(item['id']),
                     'duplicateItemId':int(item['duplicateItemId'])}
                    for item in items if int(item.get('duplicateItemId',0))>0]
        return {'success':True,'id':token_id,'pickId':token_id,
                'playerPickItemId':token_id,'playerPicks':items,'picks':items,
                'items':items,'itemData':items,'itemList':items,
                'numberItems':len(items),'availablePicks':1,
                'pickSize':len(items),'selectionSize':len(items),
                'ownership':ownership,'duplicateItemIdList':duplicates,'count':1,
                'response':{'items':items,'availablePicks':1,'ownership':ownership,
                            'duplicateItemIdList':duplicates},
                'data':{'playerPicks':items,'items':items,
                        'availablePicks':1,'ownership':ownership,
                        'duplicateItemIdList':duplicates}}

    def push_player_pick(self,reason):
        if self.game_root is None:return False
        pending=self.club.pending_player_picks()
        opened=next((pick for pick in pending if pick.get('opened')),None)
        choices={'itemData':self.player_pick_items() if opened else []}
        all_choices={'itemData':self.player_pick_items() if pending else []}
        pile=self.purchased_items()
        placeholders=','.join(str(self.club.player_pick_placeholder_id(pick['id']))
                              for pick in pending)
        try:
            (self.game_root/'pick-inject.json').write_text(json.dumps(choices),encoding='utf-8')
            (self.game_root/'pick-choices.json').write_text(json.dumps(all_choices),encoding='utf-8')
            (self.game_root/'pile-inject.json').write_text(json.dumps(pile),encoding='utf-8')
            seq=int(time.time_ns()//1_000_000)%2_000_000_000
            (self.game_root/'pick-inject.ini').write_text(
                f'[Inject]\nSeq={seq}\nPlaceholder={placeholders}\n',encoding='ascii')
        except OSError as exc:
            self.event('player_pick_inject_failed',reason=reason,error=str(exc))
            return False
        self.event('player_pick_inject',reason=reason,pending=len(pending),
                   choices=len(choices['itemData']),pile=len(pile['itemData']))
        return True

    # --- Manager ---------------------------------------------------------
    # FUT_NO_MANAGER is CardsDLL 0x1803FC38C: assetId masked to 24 bits and
    # looked up in managercards (cards_ng_db.db) by carddbid. Career-mode
    # ids (Arteta 254782, Guardiola, Klopp) are not in that table. The FUT
    # table is generic managers 1,000,195–1,263,803. Publish the extracted
    # display fields too: not every retail UI path resolves the local DB row.
    MANAGERCARDS_FILE=Path(__file__).resolve().parents[1]/'data'/'fut_managercards.json'
    DEFAULT_MANAGER_ID=1237389  # FIFA 20 starter; in managercards range

    def _managercards(self):
        cache=getattr(self,'_managercards_cache',None)
        if cache is None:
            try:
                cache=json.loads(self.MANAGERCARDS_FILE.read_text(encoding='utf-8'))
            except (OSError,ValueError):
                cache={}
            self._managercards_cache={int(k):v for k,v in cache.items()
                                      if str(k).isdigit()}
        return self._managercards_cache

    def _manager_record(self,manager_id):
        return self._managercards().get(int(manager_id)) or {}

    def _fut_manager_id(self,wanted=None):
        cards=self._managercards()
        if wanted is None:
            wanted=int(self.club.get_meta('managerId',self.DEFAULT_MANAGER_ID) or 0)
        wanted=int(wanted or 0)
        if wanted not in cards:
            wanted=self.DEFAULT_MANAGER_ID if self.DEFAULT_MANAGER_ID in cards else (
                next(iter(cards),self.DEFAULT_MANAGER_ID))
            self.club.set_meta('managerId',wanted)
        return wanted

    def manager_item(self,manager_id,item_id):
        manager_id=self._fut_manager_id(manager_id)
        record=self._manager_record(manager_id)
        league=int(record.get('leagueId') or 13)
        nation=int(record.get('nation') or 0)
        rating=int(record.get('rating') or 75)
        name=str(record.get('name') or '').strip()
        first_name=str(record.get('firstName') or '').strip()
        last_name=str(record.get('lastName') or '').strip()
        discard=max(0,(rating-30)*8)
        return {'id':int(item_id),'itemId':int(item_id),
                'assetId':int(manager_id),'definitionId':int(manager_id),
                'resourceId':int(manager_id),'resourceGameYear':2022,
                'itemType':'manager',
                'itemState':'free','rating':rating,
                'name':name,'displayName':name,'commonName':name,
                'firstName':first_name,'lastName':last_name,
                'discardValue':discard,'cardValue':discard,
                'cardsubtypeid':4,'preferredPosition':'','teamid':0,
                'leagueId':league,'leagueid':league,
                'nation':nation,'nationid':nation,
                'rareflag':int(record.get('rareflag') or 0),
                'owners':1,'contract':7,
                'untradeable':False,'undiscardable':False,
                'loans':0,'loyaltyBonus':1,
                'attributeList':[],'statsList':[],'lifetimeStats':[],
                'timestamp':int(time.time()),'year':2022,'pile':7}

    def club_manager(self):
        manager_id=self._fut_manager_id()
        self.club.ensure_manager(self.manager_item(manager_id,manager_id))
        return self.manager_item(manager_id,manager_id)

    def badge_item(self,team,item_id):
        name=next((row[1] for row in KIT_TEAMS if row[0]==int(team)),f'TeamName_{int(team)}')
        return {'id':item_id,'itemId':item_id,'assetId':int(team),
                'resourceId':6000000+int(team),'itemType':'custom',
                'itemState':'free','cardsubtypeid':11,'cardassetid':39,
                'rareflag':1,
                'category':1,'teamid':int(team),'teamId':int(team),
                'owners':1,'untradeable':True,'tradeable':False,'pile':6,
                'name':name,'displayName':name,'year':2022}

    def ball_item(self,item_id):
        # FIFA 22's vanity table identifies 23 as a licensed current ball.
        return {'id':item_id,'itemId':item_id,'assetId':23,'resourceId':23,
                'itemType':'ball','cardsubtypeid':30,'cardassetid':37,
                'itemState':'activeBall',
                'pile':7,'owners':1,'untradeable':True,'tradeable':False,
                'name':'BallName_23','year':2022}

    def worn_actives(self,prefer_owned=True):
        # FIFA 22 actives are positional: ball, badge, home kit, away kit.
        # Worn copies are pile 7. Players stay pile 6 so they do not fill
        # the 8-slot active set and block the manager.
        kit=self.onboarding_kits()
        team=10
        for item in kit['homeItemDataList']:
            if item['resourceId']==kit['homeKitId']:
                team=int(item.get('teamid') or item.get('teamId') or 10)
                home=dict(item);break
        else:
            home=dict(kit['homeItemDataList'][0]);team=int(home.get('teamid') or 10)
        away=dict(next((item for item in kit['awayItemDataList']
                        if item['resourceId']==kit['awayKitId']),
                       kit['awayItemDataList'][0]))
        try:
            badge_resource=int(self.club.get_meta('badgeId',6000001))
        except (TypeError,ValueError):
            badge_resource=6000001
        badge_team=(badge_resource-6000000
                    if badge_resource>6000000 else 1)
        badge=self.badge_item(badge_team,810000)
        home.update(pile=7,itemState='activeHomeKit',itemType='kit',
                    category=2)
        away.update(pile=7,itemState='activeAwayKit',itemType='kit',
                    category=3)
        badge.update(pile=7,itemState='activeBadge')
        fallbacks={'activeBall':self.ball_item(820000),'activeBadge':badge,
                   'activeHomeKit':home,'activeAwayKit':away,
                   'activeStadium':self.stadium_item()}
        # User-equipped club items are durable SQLite rows.  Prefer them over
        # bootstrap cosmetics so Customize survives a server/game restart.
        if prefer_owned:
            for item in self.club.list_items('club'):
                state=str(item.get('itemState',''))
                if state in fallbacks and str(item.get('itemType','')).lower() in (
                        'kit','custom','ball','stadium'):
                    fallbacks[state]=dict(item,pile=7,itemState=state)
        return [fallbacks[state] for state in
                ('activeBall','activeBadge','activeHomeKit','activeAwayKit',
                 'activeStadium')]

    def _manager_ids(self,count,seed=None):
        ids=sorted(self._managercards())
        if not ids:return [self.DEFAULT_MANAGER_ID]
        count=max(1,min(int(count),len(ids)))
        if seed is None:return ids[:count]
        return random.Random(int(seed)^0xF1FA22).sample(ids,count)

    def cpu_manager(self,seed=0,team_id=0):
        cards=self._managercards()
        ids=sorted(cards)
        league=int((sqbt.kit_of(int(team_id or 0)) or {}).get('l') or 0)
        match=[mid for mid,row in cards.items()
               if league and int((row or {}).get('leagueId') or 0)==league]
        pool=match or ids
        manager_id=pool[int(seed)%len(pool)] if pool else self.DEFAULT_MANAGER_ID
        return self.manager_item(manager_id,int(manager_id))



    def player_item_wire(self,item):
        pos=item['preferredPosition']
        face=item.get('attributeArray',[0]*6)
        trait1,trait2=TRAIT_MASKS.get(str(item['assetId']),(0,0))
        resource_id=presented_id(item['resourceId'])
        asset_id=presented_asset(item['resourceId'],item['assetId'])
        name=str(item.get('name') or '')
        wire={'id':item['id'],'itemId':item['id'],'assetId':asset_id,
                'definitionId':resource_id,'resourceId':resource_id,
                'resourceGameYear':2022,'itemType':'player','itemState':'free',
                'name':name,'lastName':(str(item.get('lastName') or '')
                                        or (name if item.get('special')
                                            else name.split(' ')[-1])),
                'rating':item['rating'],'preferredPosition':pos,'position':pos,
                'cardsubtypeid':(0 if pos=='GK' else 1 if pos in ('RB','RWB','CB','LB','LWB') else 2 if pos in ('CDM','CM','CAM') else 3),
                'teamid':item['teamid'],'leagueId':item['leagueId'],'nation':item['nationId'],
                'rareflag':item['rareflag'],'attributeArray':face,
                'trait1':trait1,'trait2':trait2,'playStyle':int(item.get('playStyle') or 0),
                'attributeList':[{'index':i,'value':v} for i,v in enumerate(face)],
                'contract':int(item.get('contract',item.get('contracts',7)) or 0),
                'contracts':int(item.get('contracts',item.get('contract',7)) or 0),
                'fitness':int(item.get('fitness',99) or 0),
                'morale':int(item.get('morale',50) or 0),'formation':'f442',
                'injuryGames':0,'injuryType':'none','suspension':0,'training':0,
                'owners':1,'loyaltyBonus':1,'loans':0,'itemState':'free',
                'untradeable':bool(item.get('untradeable',True)),
                'tradeable':bool(item.get('tradeable',False)),
                'discardValue':quicksell(item),'cardValue':quicksell(item),
                'undiscardable':bool(item.get('undiscardable',False)),'pile':6}
        # The client reads this as the folder of its per-resource action shot.
        # It must never be set for a base card: FIFA then draws dynamic artwork
        # using the gold-card crop and produces the misplaced portraits reported
        # by the game.  Face-fallback manifest entries deliberately stay on the
        # normal player-head path as well.
        is_special=bool(item.get('special')) or int(item.get('rareflag') or 0)>1
        if is_special and str(resource_id) in ACTIONSHOT_IDS:
            wire['guidAssetId']=f'd2{ACTIONSHOT_SESSION}'
        return wire

    def item_wire(self,item):
        """Serialize any owned FUT item without coercing it into a player."""
        if str(item.get('itemType','player')).lower()=='player':
            return self.player_item_wire(item)
        wire=dict(item)
        item_id=int(wire.get('id',wire.get('itemId',0)) or 0)
        wire.update(id=item_id,itemId=item_id,
                    resourceId=int(wire.get('resourceId',0) or 0),
                    assetId=int(wire.get('assetId',wire.get('resourceId',0)) or 0),
                    itemState=str(wire.get('itemState','free') or 'free'),
                    resourceGameYear=2022,
                    pile=int(wire.get('pile',6) if isinstance(wire.get('pile',6),int) else 6))
        return wire

    def consumable_collection(self):
        stacks={}
        for item in self.club.list_items('club'):
            if str(item.get('itemType','')).lower()!='development':continue
            rid=int(item.get('resourceId',0) or 0)
            current=stacks.setdefault(rid,dict(item,stackCount=0,
                                               untradeableCount=0))
            current['stackCount']+=max(1,int(item.get('stackCount',1) or 1))
            current['untradeableCount']+=max(0,int(item.get('untradeableCount',0) or 0))
        rows=[]
        for rid,item in sorted(stacks.items()):
            rows.append({'count':item['stackCount'],
                         'untradeableCount':item['untradeableCount'],
                         'resourceId':rid,
                         'discardValue':int(item.get('discardValue',0) or 0),
                         'item':self.item_wire(item)})
        return {'itemData':rows,'itemCount':len(rows),'total':len(rows),
                'count':len(rows),'start':0}

    def consumable_stats(self):
        """Native FutStickerBookStats2 response for Apply Consumable.

        ``category`` is mandatory.  FIFA's picker indexes the destination from
        it before it asks for the development-card collection; returning the
        old generic ``{}`` response crashes CardsDLL before an apply request is
        ever sent.
        """
        names=('consumablesContract','consumablesPosition',
               'consumablesTrainingGkPlayStyle',
               'consumablesTrainingPlayerPlayStyle','consumablesHealing',
               'consumablesTrainingGk','consumablesTrainingManager',
               'consumablesTrainingPlayer','consumablesContractManager',
               'consumablesContractPlayer','consumablesFitness',
               'consumablesTraining','consumablesTrainingManagerLeagueModifier',
               'consumablesFormationManager','consumablesFitnessTeam',
               'consumablesFitnessPlayer')
        totals={name:0 for name in names}
        for item in self.club.list_items('club'):
            if str(item.get('itemType','')).lower()!='development':continue
            meta=CONSUMABLE_DEFS.get(int(item.get('resourceId',0) or 0))
            if not meta:continue
            count=max(1,int(item.get('stackCount',1) or 1))
            source=str(meta.get('sourceMember',''))
            if source in totals:totals[source]+=count
            if 'Contract' in source:totals['consumablesContract']+=count
            if 'Fitness' in source:totals['consumablesFitness']+=count
            if 'Training' in source:totals['consumablesTraining']+=count
        return {'category':'consumables',
                'stat':[{'contextId':5,'contextValue':0,'type':name,
                         'typeValue':int(totals[name])} for name in names]}

    def apply_consumable(self,resource_id,target_ids):
        rid=int(resource_id);meta=CONSUMABLE_DEFS.get(rid)
        if not meta:return []
        updated=[]
        for item_id in dict.fromkeys(int(value) for value in target_ids if int(value)>0):
            item=next((row for row in self.club.list_items('club')
                       if int(row['id'])==item_id),None)
            if not item:continue
            fields={};source=str(meta.get('sourceMember',''))
            position=str(item.get('preferredPosition','')).upper()
            if source=='consumablesContractPlayer' and item.get('itemType','player')=='player':
                value=min(99,int(item.get('contract',item.get('contracts',0)) or 0)
                          +int(meta.get('amount',0) or 0))
                fields={'contract':value,'contracts':value}
            elif source=='consumablesPosition' and item.get('itemType','player')=='player':
                old,new=POSITION_MODIFIER_EFFECTS.get(rid,('',''))
                if position==old:fields={'preferredPosition':new,'position':new}
            elif source=='consumablesTrainingPlayerPlayStyle' and position!='GK':
                fields={'playStyle':int(meta['cardsubtypeid'])}
            elif source=='consumablesTrainingGkPlayStyle' and position=='GK':
                fields={'playStyle':int(meta['cardsubtypeid'])}
            if fields:
                changed=self.club.update_item_fields(item_id,fields)
                if changed:updated.append(self.item_wire(changed))
        if updated:self.club.consume_resource(rid)
        return updated

    def purchased_items(self):
        unassigned=self.club.list_items('unassigned')
        unique_types={'player','kit','custom','ball','stadium','manager'}
        owned={int(row['resourceId']):int(row['id'])
               for row in self.club.list_items('club')
               if str(row.get('itemType','player')).lower() in unique_types}
        # Recompute the native duplicate list for every unique FUT item type,
        # not players alone.  The purchase response already did this, but the
        # immediate New Items refresh dropped kit/badge/ball/stadium entries;
        # FIFA then waited forever because its duplicate count changed while
        # the pile was being committed.
        duplicates=[{'itemId':int(row['id']),
                     'duplicateItemId':owned[int(row['resourceId'])]}
                    for row in unassigned
                    if str(row.get('itemType','player')).lower() in unique_types
                    and int(row['resourceId']) in owned]
        return {'itemData':[self.item_wire(row) for row in unassigned],
                'duplicateItemIdList':duplicates}

    def market_auction_wire(self,trade):
        item=self.player_item_wire(dict(trade['card'],id=trade['itemId'] or trade['tradeId']))
        item.update(untradeable=False,tradeable=True,
                    pile=6 if trade['state']=='sold' else 0,
                    id=trade['itemId'] or trade['tradeId'],
                    itemId=trade['itemId'] or trade['tradeId'])
        return {'tradeId':trade['tradeId'],'itemData':item,
                'tradeState':'closed' if trade['state']=='sold' else trade['state'],
                'bidState':trade.get('bidState','none'),
                'startingBid':trade.get('startingBid',max(100,trade['price']//3)),
                'buyNowPrice':trade['price'],'currentBid':trade['bid'],
                'coinsProcessed':trade['bid'] if trade['state']=='sold' else 0,
                'expires':trade['expires'],'watched':trade.get('watched',False)}

    def transfer_auction_wire(self,listing):
        item=self.player_item_wire(listing['card'])
        return {'tradeId':listing['tradeId'],'itemData':item,
                'tradeState':listing['state'],'bidState':'none',
                'startingBid':listing['startingBid'],
                'buyNowPrice':listing['buyNowPrice'],'currentBid':listing.get('bid',0),
                'coinsProcessed':listing.get('coinsProcessed',0),'expires':listing['expires'],
                'watched':False}

    def _finish_squad(self,squad):
        """Bring a squad to the live-proved FutSquadLoadServerResponse shape.

        The fifa22work build documents the fields the client's squad parser
        and the match-start validation expect: rank on every slot, captain =
        first filled item, changed/custom/tactics, kicktakers as {index,id,
        dream} objects, and the seated manager with pile 7 — 'an assigned
        manager is an active item; an item outside pile 7 is simply not
        active', which is exactly the 'You must assign a Manager' blocker.
        """
        players=squad.get('players',[])
        filled=[]
        for index,entry in enumerate(players):
            entry.setdefault('rank',0)
            entry.setdefault('kitNumber',index+1 if index<11 else 0)
            card=entry.get('itemData')
            if card:
                filled.append(int(card.get('id') or 0))
        squad['captain']=filled[0] if filled else 0
        squad['changed']=0
        squad.setdefault('custom','[0,0,0,0,0,0,0,0,0,0,0]')
        squad['tactics']=[]
        squad['kicktakers']=[{'index':i,'id':item_id,'dream':False}
                             for i,item_id in enumerate(filled[:5])]
        managers=squad.get('manager')
        if isinstance(managers,list):
            squad['manager']=[dict(row,pile=7) for row in managers
                              if isinstance(row,dict)]
        return squad

    def regular_squad(self,squad_id=None,*,items=None,club=None):
        """Build one complete, directly loadable FIFA 22 squad document."""
        club=club or self.club.status()
        items=items if items is not None else self.club.list_items()
        active_id=self.club.active_squad_id()
        squad_id=active_id if squad_id is None or int(squad_id)==0 else int(squad_id)
        metadata=self.club.squad_metadata(squad_id)
        by_id={int(item['id']):item for item in items}
        slots=self.club.squad(squad_id)
        players=[{'index':i,'loyaltyBonus':1,'kitNumber':i+1 if i<11 else 0,
                  **({'itemData':self.player_item_wire(by_id[item_id])}
                     if item_id in by_id else {})}
                 for i,item_id in enumerate(slots)]
        actives=self.worn_actives()
        worn_badge=next((item for item in actives
                         if item.get('itemState')=='activeBadge'),{})
        badge_team=int(worn_badge.get('teamid') or worn_badge.get('teamId') or 0)
        manager=self.club_manager()
        starters=[entry['itemData'] for entry in players[:11] if entry.get('itemData')]
        squad_rating=(int(round(sum(int(card.get('rating',0)) for card in starters)/11))
                      if len(starters)==11 else 0)
        squad={'id':squad_id,'squadId':squad_id,'personaId':1000000022,
               'name':metadata['name'],'squadName':metadata['name'],
               'clubName':club['clubName'],'teamId':badge_team,'clubId':badge_team,
               'badgeId':badge_team,'badgeAssetId':badge_team,
               'badgeResourceId':int(worn_badge.get('resourceId') or 0),
               'formation':metadata['formation'],'rating':squad_rating,
               'starRating':squad_rating,'squadType':'REGULAR_SQUAD',
               'active':squad_id==active_id,'valid':len(starters)==11,
               'chemistry':100 if len(starters)==11 else 0,'players':players,
               'actives':actives,'club':actives,
               'myStadiumInfo':self.stadium_document(),'manager':[manager],
               'kicktakers':[],'newSquad':0,'newsquad':0}
        return self._finish_squad(squad)

    def squad_list_payload(self):
        items=self.club.list_items();club=self.club.status()
        return {'activeSquadId':self.club.active_squad_id(),
                # CardsDLL treats list entries as full squads and immediately
                # reads all 23 slots; summaries crash the native selector.
                'squad':[self.regular_squad(sid,items=items,club=club)
                         for sid in self.club.squad_ids()]}

    def save_regular_squad(self,payload,*,squad_id=None,create=False):
        """Persist a native squad create/edit without mixing saved lineups."""
        source=payload if isinstance(payload,dict) else {}
        if isinstance(source.get('squad'),dict):source=source['squad']
        source=dict(source)
        if create:
            squad_id=self.club.next_squad_id()
        elif squad_id is None:
            try:squad_id=int(source.get('id') or source.get('squadId') or 0)
            except (TypeError,ValueError):squad_id=0
            if squad_id<=0:squad_id=self.club.active_squad_id()
        squad_id=int(squad_id)
        exists=squad_id in self.club.squad_ids()
        current=self.club.squad(squad_id) if exists else [0]*23
        supplied_players=source.get('players')
        # Name-only/formation-only updates do not erase a lineup.  A real
        # player array, including a deliberately empty 23-slot array, does.
        if isinstance(supplied_players,list) and supplied_players:
            slots=[0]*23
            for order,row in enumerate(supplied_players[:64]):
                if not isinstance(row,dict):continue
                try:index=int(row.get('index',order))
                except (TypeError,ValueError):continue
                nested=row.get('itemData') if isinstance(row.get('itemData'),dict) else {}
                try:item_id=int(row.get('itemId') or nested.get('id') or
                                nested.get('itemId') or row.get('id') or 0)
                except (TypeError,ValueError):item_id=0
                if 0<=index<23:slots[index]=max(0,item_id)
        elif create:
            slots=[0]*23
        else:
            slots=list(current)
        self.club.save_squad(squad_id,slots)
        name=source.get('squadName',source.get('name'))
        formation=source.get('formation')
        self.club.set_squad_metadata(squad_id,name=name,formation=formation)

        managers=source.get('manager',[])
        if isinstance(managers,dict):managers=[managers]
        if isinstance(managers,list) and managers and isinstance(managers[0],dict):
            row=managers[0]
            nested=row.get('itemData') if isinstance(row.get('itemData'),dict) else {}
            try:wanted=int(row.get('id') or nested.get('id') or 0)
            except (TypeError,ValueError):wanted=0
            if wanted>0:
                owned=next((item for item in self.club.list_items('club')
                            if item.get('itemType')=='manager'
                            and wanted in (int(item['id']),
                                           int(item.get('resourceId') or 0),
                                           int(item.get('assetId') or 0))),None)
                if owned:
                    self.club.set_meta('managerId',int(owned.get('resourceId')
                                                        or owned.get('assetId')
                                                        or self.DEFAULT_MANAGER_ID))
        if create or bool(source.get('active',False)):
            self.club.set_active_squad(squad_id)
        saved=self.regular_squad(squad_id)
        self.event('squad_saved',squadId=squad_id,
                   filled=sum(1 for slot in slots if slot),
                   created=bool(create),active=bool(saved.get('active')),
                   name=saved.get('squadName'),formation=saved.get('formation'))
        return saved

    def user_mass_info(self):
        club=self.club.status()
        items=self.club.list_items()
        player_count=sum(item.get('itemType','player')=='player' for item in items)
        credits=club['credits']
        actives=self.worn_actives()
        squad=self.regular_squad(items=items,club=club)
        squad_list=self.squad_list_payload()
        info={'personaId':1000000022,'personaName':'LocalPlayer',
              'clubId':1,'clubName':club['clubName'],'clubAbbr':club['clubAbbr'],
              'clubNameChangeAllowed':not club['clubNamed'],
              'credits':credits,'currencies':[{'name':'COINS','funds':credits,'finalFunds':credits},
                                              {'name':'POINTS','funds':0,'finalFunds':0},
                                              {'name':'DRAFT_TOKEN','funds':0,'finalFunds':0}],
              'won':0,'draw':0,'loss':0,'gamesPlayed':0,'divisionOffline':10,
              'divisionOnline':10,'purchased':True,'returningUser':1,
              'hasClub':True,'onboardingComplete':1,'firstTime':False,
              'clubPlayers':player_count,'ownedPlayers':player_count,'playerCount':player_count,
              'clubItemCount':len(items),'clubCount':len(items),'actives':actives,
              'squadList':squad_list,
              'unopenedPacks':self.unopened_packs_payload(),
              'bidTokens':{'count':0,'updateTime':0}}
        return self._with_objectives({'userInfo':info,'squad':squad,'loanPlayers':{'loans':[]},
                'loanPlayerClientData':{'entries':[{'key':0,'value':items[0]['assetId']&0xffffff}]
                                        if items else []},
                'onboardingClientData':self.completed_onboarding_client_data(),
                'credits':credits,'purchasedItems':self.purchased_items(),
                'pileSizeClientData':{'entries':[]},'activeMessages':{'activeMessage':[]}})

    def completed_onboarding_client_data(self):
        """Native completed wizard state, matching the proven FIFA 20 flow."""
        equipped={str(item.get('itemState','')):int(item.get('resourceId',0) or 0)
                  for item in self.worn_actives()}
        return {'entries':[
            {'key':0,'value':3},
            {'key':1,'value':equipped.get('activeHomeKit',6300001)},
            {'key':2,'value':equipped.get('activeAwayKit',6400001)},
            {'key':3,'value':equipped.get('activeBadge',6000001)}]}

    def respond(self,packet,fields):
        key=packet.component,packet.command
        if key==(9,2):return integer('STIM',int(time.time())),'Util.Ping'
        if key==(9,7):
            config={'connIdleTimeout':'120s','defaultRequestTimeout':'80s','pingPeriod':'20s',
                    'voipHeadsetUpdateRate':'1000','xlspConnectionIdleTimeout':'300',
                    'blazeServerClientId':'FOS-SERVER','blazeSdkClientId':'FOS-SERVER','blazeSdkClientSecret':'',
                    'nucleusConnect':f'http://127.0.0.1:{self.http_port}/',
                    'identityRedirectUri':f'http://127.0.0.1:{self.http_port}/localfut/callback'}
            # Keep the earlier no-QoS experiment as the default. The option to
            # advertise QoS now points to a distinct listener, matching the
            # reference topology; compare both modes in a future game trace.
            bandwidth_site=text('PSA','127.0.0.1')+integer('PSP',self.qos_port)+text('SNA','qos')
            latency_site=text('PSA','127.0.0.1')+integer('PSP',self.qos_port)+text('SNA','ea-sjc')
            qoss=group('BWPS',bandwidth_site)+integer('LNP',10)+group_map('LTPS',{'ea-sjc':latency_site})+integer('SVID',1161889797)+integer('TIME',5000)
            payload=(text('ASRC','303107')+integer_list('CIDS',[1,9,35,30722])+group('CONF',string_map('CONF',config))
                    +integer('EEFA',1)+text('ESRC','303107')+text('INST','fifa-2022-pc')+integer('MAID',89)
                    +integer('MINR',0)+text('NASP','cem_ea_id')+text('PILD','')+text('PLAT','pc'))
            if self.preauth_qos:payload+=group('QOSS',qoss)
            payload+=text('RSRC','303107')+text('SVER','Blaze 15.1.1.10.3')
            return payload,'Util.PreAuth (QoS '+('on' if self.preauth_qos else 'off')+')'
        if key in ((9,1),(9,30)):
            cfid=find(fields,'CFID','')
            if not isinstance(cfid,str):raise ProtocolError('CFID must be a string')
            # FIFA 22 requests FIFA_CORE during boot, whereas the older
            # reference client requests OSDK_CORE. Both need the local FUT
            # endpoint map for the title to discover the HTTP service.
            settings=self.core_config() if cfid in ('OSDK_CORE','FIFA_CORE') else {}
            return string_map('CONF',settings)+text('ID',cfid)+text('ITYP','')+text('TOKN',''),f'Util.FetchClientConfig({cfid}; provisional)'
        if key==(9,8):return self.post_auth(),'Util.PostAuth (reference-shaped, unverified)'
        if key==(9,5):return self.telemetry(),'Util.GetTelemetryServer (reference-shaped, unverified)'
        if key==(9,20):
            # FIFA checks each proposed club/squad name here before submitting
            # the corresponding FUT HTTP request. The observed TLST contains
            # DIRT=2 (unprocessed) and UTXT for each proposed string.
            rows=find(fields,'TLST')
            if not isinstance(rows,list) or len(rows)>32:
                raise ProtocolError('Unexpected profanity text list')
            accepted=[]
            for row in rows:
                if not isinstance(row,list):
                    raise ProtocolError('Unexpected profanity text entry')
                value=find(row,'UTXT')
                if not isinstance(value,str) or len(value)>256:
                    raise ProtocolError('Unexpected profanity text')
                accepted.append(integer('DIRT',0)+text('UTXT',value))
            return group_list('TLST',accepted),'Util.FilterForProfanity'
        if key==(9,12):return string_map('SMAP',{'FirstTimeFlag':'0'}),'Util.GetUserOptions'
        if key==(9,10):
            option=find(fields,'KEY','')
            if not isinstance(option,str):raise ProtocolError('KEY must be a string')
            return text('DATA','0' if option=='FirstTimeFlag' else '')+text('KEY',option),'Util.GetUserOption'
        if key in ((9,22),(9,23),(9,28)):
            return b'','Util.ClientState (reference fieldless response)'
        if key==(1,10):
            if find(fields,'AUTH') is None or find(fields,'CTYP') is None:
                raise ProtocolError('Unexpected FIFA 22 login request shape')
            return self.local_login(),'Authentication.LocalLogin (reference-shaped, unverified)'
        if key==(1,70):return b'','Authentication.Logout (observed FIFA22 request; reference response)'
        if key==(30722,50):
            # FIFA 22 asks for platform persona names after match creation.
            # This local session has no records for those remote identities.
            namespace=find(fields,'NASP')
            names=find(fields,'PLST')
            if not isinstance(namespace,str) or not isinstance(names,list) or len(names)>100:
                raise ProtocolError('Unexpected persona-name lookup')
            if any(not isinstance(name,str) for name in names):
                raise ProtocolError('Unexpected persona name')
            return group_list('ULST',[]),'UserSessions.LookupUsersByPersonaNames (no local matches)'
        if key==(30722,20):
            return self.login_notifications()[2][2],'UserSessions.UpdateNetworkInfo (reference-shaped, unverified)'
        if key==(30722,8):
            return self.login_notifications()[1][2],'UserSessions.GetLoginSession (reference-shaped, unverified)'
        if key==(15,2):
            return integer('MCNT',0),'Messaging.FetchMessages (typed empty count)'
        if key==(10,5):
            return b'','Census.Subscribe (reference fieldless response)'
        if key==(11,2600):
            return (integer('CLDS',0)+integer('MXEV',0)+integer('MXRV',0)
                    +integer('PUHR',0)+integer('SOVR',0)+integer('STRT',0)), 'Clubs.GetComponentSettings (typed defaults)'
        if key==(11,1600):
            return group_list('CIST',[]),'Clubs.GetInvitations (typed empty list)'
        if key==(25,6):
            return group_list('LMAP',[]),'AssociationLists.GetLists (typed empty list)'
        if key in ((21,10),(10,1)):
            return b'',f'Bootstrap {key[0]}/{key[1]} (reference fieldless response)'
        if key==(2249,1):
            setting=text('ID','O_TKfilter')+integer('LOCF',0)+integer('TOGG',0)
            return group_list('LSST',[setting]),'Ticker.GetSettings (reference-shaped)'
        if key==(2249,2):
            return group_list('LGRP',[text('ID','O_SG_TCKR')+string_list('LSET',['O_TKfilter'])]),'Ticker.GetGroups (reference-shaped)'
        if key==(7,15):
            return empty_map('KSIT',1,3),'Stats.GetKeyScopesMap (reference-shaped)'
        if key==(7,3):
            return group_list('GRPS',[]),'Stats.GetStatGroupList (reference-shaped)'
        if key==(7,20):
            return self.period_ids(),'Stats.GetPeriodIds (reference-shaped)'
        if key==(1,32):
            return self.entitlements(fields),'Authentication.ListEntitlements (FIFA22 grant)'
        if key==(2076,3):
            # The title appends this field to CONTENT_URL; an absolute URL was
            # observed on the wire as /contenthttp://..., so keep it relative.
            return text('URL','sponsored-events'),'SponsoredEvents.GetEventsURL (relative content path)'
        # Authentication and FUT payloads will be implemented from the observed
        # FIFA 22 exchange. Do not present fabricated successful login sessions.
        return None,'Unimplemented native command'

class BlazeServer(socketserver.ThreadingTCPServer):
    # Windows SO_REUSEADDR permitted two live local services to own the same
    # port and split requests between different code revisions.
    allow_reuse_address=False
    daemon_threads=True

class BlazeHandler(socketserver.BaseRequestHandler):
    def handle(self):
        runtime=self.server.runtime
        connection_id=secrets.token_hex(8)
        runtime.event('blaze_connection',connection_id=connection_id)
        # FUT menu setup can take longer than thirty seconds between Blaze
        # calls while the title loads its HTTP configuration and art.
        self.request.settimeout(120)
        buffer=bytearray()
        try:
            while True:
                packet=take_packet(buffer)
                if packet is None:
                    chunk=self.request.recv(65536)
                    if not chunk:return
                    buffer.extend(chunk)
                    continue
                fields,error,recorded=runtime.capture(packet,connection_id)
                if packet.kind==4:
                    self.request.sendall(Packet(packet.component,packet.command,packet.message,5).encode())
                    continue
                if packet.kind!=0:continue
                if error:
                    runtime.event('unsupported_tdf',connection_id=connection_id,component=packet.component,command=packet.command,message=packet.message)
                    return
                payload,label=runtime.respond(packet,fields)
                if payload is None:
                    runtime.event('unimplemented_blaze',connection_id=connection_id,component=packet.component,command=packet.command,message=packet.message)
                    return
                self.request.sendall(Packet(packet.component,packet.command,packet.message,1,payload).encode())
                if recorded:
                    runtime.event('blaze_response',connection_id=connection_id,component=packet.component,command=packet.command,message=packet.message,bytes=len(payload),label=label)
                if (packet.component,packet.command)==(1,10):
                    for component,command,notice,notice_label in runtime.login_notifications():
                        self.request.sendall(Packet(component,command,0,2,notice).encode())
                        runtime.event('blaze_notification',connection_id=connection_id,component=component,command=command,bytes=len(notice),label=notice_label)
        except (OSError,ProtocolError) as exc:
            runtime.event('blaze_error',connection_id=connection_id,error=str(exc))

class HttpServer(ThreadingHTTPServer):
    daemon_threads=True
    allow_reuse_address=False

class HttpHandler(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def log_message(self,*args):pass
    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def send(self,status,payload,content_type='application/json',extra_headers=None):
        if not isinstance(payload,bytes):payload=json.dumps(payload).encode()
        path=urlsplit(self.path).path
        if path.startswith(('/ut/','/fut/')):
            self.server.runtime.event('fut_http_response',request_id=getattr(self,'request_id',None),method=self.command,path=path,
                                      status=status,body_bytes=len(payload))
        self.send_response(status)
        self.send_header('Content-Type',content_type)
        self.send_header('Content-Length',str(len(payload)))
        if not any(str(name).lower()=='cache-control'
                   for name in (extra_headers or {})):
            self.send_header('Cache-Control','no-store')
        self.send_header('Connection','close')
        for name,value in (extra_headers or {}).items():self.send_header(name,value)
        self.end_headers()
        self.wfile.write(payload)
        self.close_connection=True

    def go(self):
        runtime=self.server.runtime
        self.request_id=secrets.token_hex(8)
        path=urlsplit(self.path).path
        try:
            if self.headers.get('Transfer-Encoding'):
                self.send(400,{'error':'Transfer-Encoding not supported'});return
            size=int(self.headers.get('Content-Length','0'))
            if not 0<=size<=1024*1024:
                self.send(413,{'error':'Request body too large'});return
            body=self.rfile.read(size)
            if len(body)!=size:
                self.send(400,{'error':'Incomplete request body'});return
        except (ValueError,OSError):
            self.send(400,{'error':'Invalid request body'});return
        # Log method/path/body length, never authorization headers or token query values.
        runtime.event('http_request',request_id=self.request_id,method=self.command,path=path,body_bytes=len(body))
        if path in ('/ut/auth','/ut/ut/auth') and body:
            try:
                document=json.loads(body)
                runtime.event('fut_auth_schema',format='json',
                              keys=sorted(document) if isinstance(document,dict) else [],
                              value_type=type(document).__name__)
            except (ValueError,UnicodeDecodeError):
                runtime.event('fut_auth_schema',format='non-json',body_bytes=len(body))
        if self.server.is_redirector:
            runtime.event('redirector_request',path=path)
            xml=(f'<?xml version="1.0" encoding="UTF-8"?><serverinstanceinfo><address member="0"><valu>'
                 f'<hostname>127.0.0.1</hostname><ip>2130706433</ip><port>{runtime.blaze_port}</port>'
                 f'</valu></address><secure>0</secure></serverinstanceinfo>').encode()
            self.send(200,xml,'application/xml');return
        if path=='/localfut22/status' and self.command=='GET':
            with runtime.lock:counts=dict(runtime.counts)
            self.send(200,{'stage':'club-bring-up','futMenuVerified':True,
                           'nativeLoginImplemented':False,'nativeMenusImplemented':False,
                           'redirectorTls':runtime.redirector_tls,
                           'serverSourceSha256':runtime.server_source_sha256,
                           'club':runtime.club.status(),'events':counts});return
        if self.server.is_qos and path=='/qos/qos' and self.command=='GET':
            # Reference-derived zero-probe local response. Native QoS acceptance
            # remains a separate step to verify in the game.
            xml=(f'<?xml version="1.0" encoding="utf-8"?><qos><numprobes>0</numprobes>'
                 f'<qosport>{runtime.qos_port}</qosport><probesize>0</probesize><qosip>16777343</qosip>'
                 f'<requestid>1</requestid><reqsecret>0</reqsecret></qos>').encode()
            self.send(200,xml,'application/xml');return
        if self.server.is_qos and path in ('/qos/firewall','/qos/firetype') and self.command=='GET':
            xml=(f'<?xml version="1.0" encoding="utf-8"?><firewall><ips><ips>16777343</ips><ips>16777343</ips></ips>'
                 f'<numinterfaces>2</numinterfaces><ports><ports>{runtime.qos_port}</ports>'
                 f'<ports>{runtime.qos_port}</ports></ports><requestid>1234</requestid><reqsecret>5678</reqsecret></firewall>').encode()
            self.send(200,xml,'application/xml');return
        if path in ('/roster','/routing','/ctl') and self.command=='GET':
            # The FIFA 18 local service answers these update probes with an
            # empty successful download. Validate FIFA 22's behavior live.
            self.send(200,b'','application/octet-stream');return
        if path in ('/content/fifa/dime/gen4/dimecfg.xml',
                    '/content/fifa/dime/gen4/storecfg.xml') and self.command=='GET':
            self.send(200,b'','application/octet-stream');return
        if path in ('/sponsored-events','/content/sponsored-events') and self.command=='GET':
            self.send(204,b'','text/plain');return
        if path in ('/ut/auth','/ut/ut/auth'):
            if self.command=='DELETE':self.send(200,{});return
            if self.command=='POST':
                session=runtime.token
                self.send(200,{'sid':session,'protocol':'http',
                               'ipPort':f'127.0.0.1:{runtime.http_port}'},
                          extra_headers={'X-UT-SID':session,'X-POW-SID':session})
                return
        if path=='/ut/game/fifa22/user/club' and self.command=='PUT':
            try:document=json.loads(body)
            except (ValueError,UnicodeDecodeError):
                self.send(400,{'error':'INVALID_CLUB_NAME'});return
            if not isinstance(document,dict):
                self.send(400,{'error':'INVALID_CLUB_NAME'});return
            try:runtime.club.rename(document.get('clubName'),document.get('clubAbbr'))
            except StateError:
                self.send(400,{'error':'INVALID_CLUB_NAME'});return
            runtime.event('club_renamed')
            # FutChangeClubNameServerResponse expects an object with no fields.
            self.send(200,{})
            return
        if path=='/ut/game/fifa22/v2/user/accountinfo' and self.command=='GET':
            self.send(200,runtime.account_info(),extra_headers={'X-UT-SID':runtime.token})
            return
        if path=='/ut/game/fifa22/settings' and self.command=='GET':
            self.send(200,runtime.fut_settings(),extra_headers={'X-UT-SID':runtime.token})
            return
        if path=='/ut/game/fifa22/resourceMapping' and self.command=='GET':
            # CardsDLL enters a top-level array before checking EndArray. A 204
            # has no JSON document and leaves the shared content promise pending;
            # Store, SBC and Objectives then stall before their own requests.
            self.send(200,[],'application/json',
                      {'Cache-Control':'no-store, no-cache, must-revalidate'})
            return
        if path=='/ut/game/fifa22/match/reset' and self.command=='PUT':
            self.send(200,{'reset':True})
            return
        if path=='/ut/game/fifa22/match' and self.command in ('POST','PUT'):
            try:payload=json.loads(body) if body else {}
            except (ValueError,UnicodeDecodeError):
                self.send(400,{'error':'INVALID_MATCH_BODY'});return
            if not isinstance(payload,dict):
                self.send(400,{'error':'INVALID_MATCH_BODY'});return
            runtime.event('sqbt_match_request',method=self.command,
                          keys=sorted(payload),
                          bodyPreview=json.dumps(payload,separators=(',',':'))[:500])
            if self.command=='PUT':
                # FIFA 22 sends this after kit selection as MatchReady. It is
                # not another CreateMatch and must not enter the Draft route.
                draft=runtime._draft_load_state()
                draft_active=(draft or {}).get('activeMatch') or {}
                if draft_active:
                    gid=runtime._sqbt_find_int(payload,'gid','gameId',default=0)
                    if gid>0:
                        draft_active['gid']=gid;draft['activeMatch']=draft_active
                        runtime._draft_save_state(draft)
                    runtime.event('draft_match_ready',gid=gid,
                                  opponentSquadId=draft_active.get('opponentSquadId'))
                    squad=runtime.draft_squad(draft)
                    actives=runtime.worn_actives();squad['actives']=actives;squad['club']=actives
                    difficulty=int(draft.get('difficulty',3) or 3)
                    difficulty_name=next((name for value,name,_ in
                                          runtime.DRAFT_DIFFICULTIES
                                          if value==difficulty),'PRO')
                    squad.update(matchDifficulty=difficulty,
                                 difficulty=difficulty,
                                 difficultyId=difficulty,
                                 gameDifficulty=difficulty,
                                 sqbtMatchDifficulty=difficulty,
                                 difficultyName=('PROFESSIONAL'
                                                 if difficulty_name=='PRO'
                                                 else difficulty_name))
                    self.send(200,{'startDateTime':int(draft_active.get('matchStart')
                                                       or time.time()),
                                   'matchmakingElo':0,'mismatches':[],
                                   'champs2WinForm':0,'rivalsV2StageId':0,
                                   'squad':squad,
                                   'opponentSquadId':int(draft_active.get(
                                       'opponentSquadId',runtime.DRAFT_OPPONENT_SQUAD_ID)),
                                   'matchDifficulty':difficulty,
                                   'difficulty':difficulty,
                                   'difficultyId':difficulty,
                                   'gameDifficulty':difficulty,
                                   'sqbtMatchDifficulty':difficulty,
                                   'difficultyName':('PROFESSIONAL'
                                                     if difficulty_name=='PRO'
                                                     else difficulty_name)})
                    return
                state=runtime._sqbt_load_state()
                active=state.get('activeMatch') or {}
                gid=runtime._sqbt_find_int(payload,'gid','gameId',default=0)
                if active and gid>0:
                    active['gid']=gid
                    state['activeMatch']=active
                    runtime._sqbt_save_state(state)
                runtime.event('sqbt_match_ready',gid=gid,
                              active=bool(active))
                self.send(200,{'startDateTime':int(active.get('matchStart')
                                                    or time.time()),
                               'matchmakingElo':0,'mismatches':[],
                               'champs2WinForm':0,'rivalsV2StageId':0,
                               'squad':runtime.user_mass_info()['squad']})
                return
            end_markers=('reason','endReason','result','goals','goalsAgainst',
                         'secondsPlayed','matchResult')
            if any(key in payload for key in end_markers) and self.command=='POST':
                # Match the last live-proven FIFA 22 flow: a Draft report must
                # be offered to Draft first.  A stale Squad Battles activeMatch
                # otherwise consumes a Draft forfeit and freezes the transition.
                draft_end=runtime.draft_match_end(payload)
                if draft_end is not None:
                    self.send(200,draft_end);return
                sqbt_state=runtime._sqbt_load_state()
                if sqbt_state.get('activeMatch') or any(key in payload for key in (
                        'sqbtOpponentSquadId','sqbtOppid','sqbtEventId',
                        'opponentSquadId','selectedOpponentId')):
                    self.send(200,runtime.squad_battles_match_end(payload));return
                self.send(200,runtime.generic_match_end(payload));return
            # Live 2026-09-21: Squad Battles CreateMatch is
            # {squadId:1, type:OFFLINE, sqbtEventId, sqbtOpponentSquadId, ...}.
            # A leftover Draft in READY_FOR_MATCH used to steal that request,
            # so the preview became Crystal Palace vs "Draft Rival 1" and the
            # manager check ran on the draft CPU squad.
            sqbt_match=('sqbtOpponentSquadId' in payload or 'sqbtEventId' in payload
                        or 'sqbtOppid' in payload)
            squad_id=runtime._sqbt_find_int(payload,'squadId','squadid',default=0)
            draft_state=runtime._draft_load_state()
            if (not sqbt_match) and (
                    squad_id>=runtime.DRAFT_SQUAD_ID or
                    (draft_state and draft_state.get('draftState')=='READY_FOR_MATCH')):
                result=runtime.draft_match_start(payload)
                if result is not None:
                    self.send(200,result);return
            result=runtime.squad_battles_match_start(payload)
            if result is None:
                # An RS4 404 disconnects this client. Empty mismatches still
                # lets CreateMatch finish so the user can back out.
                self.send(200,{'startDateTime':int(time.time()),
                               'matchmakingElo':0,'mismatches':[],
                               'champs2WinForm':0,'squad':runtime.user_mass_info()['squad']})
                return
            self.send(200,result);return
        match_ready=re.fullmatch(r'/ut/game/fifa22/match(?:/\d+)?/ready',path)
        if match_ready and self.command in ('GET','POST','PUT'):
            self.send(200,{})
            return
        if path in ('/ut/game/fifa22/match/end','/ut/game/fifa22/match/report') and self.command in ('POST','PUT'):
            try:payload=json.loads(body) if body else {}
            except (ValueError,UnicodeDecodeError):
                self.send(400,{'error':'INVALID_MATCH_END'});return
            if not isinstance(payload,dict):
                self.send(400,{'error':'INVALID_MATCH_END'});return
            runtime.event('sqbt_match_end_request',method=self.command,
                          keys=sorted(payload),
                          bodyPreview=json.dumps(payload,separators=(',',':'))[:500])
            runtime.event('sqbt_match_end_stats',
                          myMatchStats=payload.get('myMatchStats'),
                          opponentMatchStats=payload.get('opponentMatchStats'),
                          matchData=payload.get('matchData'),
                          events=payload.get('events'),
                           scoredByItems=[{'id':row.get('id'),'goals':row.get('goals')}
                                          for row in payload.get('items',[])
                                          if isinstance(row,dict) and row.get('goals')])
            sqbt_state=runtime._sqbt_load_state()
            if sqbt_state.get('activeMatch'):
                self.send(200,runtime.squad_battles_match_end(payload));return
            draft_end=runtime.draft_match_end(payload)
            if draft_end is not None:
                self.send(200,draft_end);return
            if any(key in payload for key in (
                    'sqbtOpponentSquadId','sqbtOppid','sqbtEventId',
                    'opponentSquadId','selectedOpponentId')):
                self.send(200,runtime.squad_battles_match_end(payload));return
            self.send(200,runtime.generic_match_end(payload));return
        if path=='/ut/game/fifa22/user/friends' and self.command=='POST':
            self.send(200,{'friends':[]})
            return
        # FIFA 22 Squad Battles follows opponentsquad with several casings of
        # this route. A 501 on the lowercase form left the client retrying forever.
        if self.command=='GET' and path.lower() in (
                '/ut/game/fifa22/usermassinfo','/ut/usermassinfo'):
            self.send(200,runtime.user_mass_info(),extra_headers={'X-UT-SID':runtime.token})
            return
        if path=='/ut/game/fifa22/user' and self.command=='GET':
            self.send(200,runtime.user_mass_info()['userInfo'],
                      extra_headers={'X-UT-SID':runtime.token})
            return
        # --- Offline Draft (CardsDLL family routes; array roots are proven) ---
        if path in ('/ut/game/fifa22/squad/mode/draft/state',
                    '/ut/game/fifa22/draft/mode/draft/state') and self.command=='GET':
            state=runtime._draft_load_state()
            self.send(200,[runtime.draft_wire_state(runtime.draft_state_payload(state))])
            return
        draft_purchase=re.fullmatch(
            r'/ut/game/fifa22/(?:(?:squad|draft)/mode/)?purchase/mode/(\d+)/draft',path)
        if draft_purchase and self.command=='POST':
            try:payload=json.loads(body) if body else {}
            except (ValueError,UnicodeDecodeError):payload={}
            if not isinstance(payload,dict):payload={}
            self.send(200,[runtime.draft_purchase(int(draft_purchase[1]))])
            return
        draft_choose_diff=re.fullmatch(
            r'/ut/game/fifa22/(?:squad|draft)/mode/(?:\d+/)?draft/choose/difficulty',
            path)
        if draft_choose_diff and self.command in ('PUT','POST'):
            try:payload=json.loads(body) if body else {}
            except (ValueError,UnicodeDecodeError):payload={}
            self.send(200,[runtime.draft_choose_difficulty(payload)])
            return
        draft_choices=re.fullmatch(
            r'/ut/game/fifa22/(?:squad|draft)/mode/(?:\d+/)?draft/choices/'
            r'(player|captain|formation|difficulty|manager)',path)
        if draft_choices and self.command in ('GET','PUT','POST'):
            try:payload=json.loads(body) if body else {}
            except (ValueError,UnicodeDecodeError):payload={}
            slot=runtime._sqbt_find_int(payload,'positionId','positionid','slot',
                                        'choiceIndex',default=0)
            self.send(200,runtime.draft_choices(draft_choices[1].upper(),slot))
            return
        # The game puts the draft session id in the path: /squad/mode/1/draft/choose.
        draft_choose=re.fullmatch(
            r'/ut/game/fifa22/(?:squad|draft)/mode/(?:\d+/)?draft/choose',path)
        if draft_choose and self.command in ('PUT','POST'):
            try:payload=json.loads(body) if body else {}
            except (ValueError,UnicodeDecodeError):
                self.send(400,{'error':'INVALID_DRAFT_CHOOSE'});return
            self.send(200,[runtime.draft_choose(payload)])
            return
        draft_auto=re.fullmatch(
            r'/ut/game/fifa22/(?:squad|draft)/mode/(?:\d+/)?draft/autocomplete',path)
        if draft_auto and self.command in ('POST','PUT'):
            self.send(200,runtime.draft_autofill_response())
            return
        draft_stats=re.fullmatch(
            r'/ut/game/fifa22/(?:squad|draft)/mode/(?:\d+/)?(?:draft/mode/)?(\d+)/stats',path)
        if draft_stats and self.command=='GET':
            self.send(200,runtime.draft_stats())
            return
        if path in ('/ut/game/fifa22/squad/mode/draft',
                    '/ut/game/fifa22/draft/mode/draft'):
            if self.command=='DELETE':
                runtime.draft_abandon()
                self.send(200,b'',content_type='application/json');return
            if self.command=='GET':
                state=runtime._draft_load_state()
                self.send(200,[runtime.draft_wire_state(
                    runtime.draft_state_payload(state))])
                return
        if path.endswith('/grant/award') and self.command in ('GET','POST','PUT'):
            claimed=runtime.draft_claim_award()
            if claimed is None:self.send(200,b'',content_type='application/json')
            else:self.send(200,claimed[0])
            return
        # --- Leaderboards, transfer market, store, wallet -------------------
        if path=='/ut/game/fifa22/leaderboards/options' and self.command=='GET':
            options=[{'id':'sqbt','type':'SQUAD_BATTLES','name':'Squad Battles','enabled':True},
                     {'id':'rivals','type':'RIVALS','name':'Division Rivals','enabled':False},
                     {'id':'champions','type':'CHAMPIONS','name':'FUT Champions','enabled':False}]
            self.send(200,{'options':options,'leaderboardOptions':options,
                           'items':options})
            return
        if path=='/ut/game/fifa22/transfermarket' and self.command=='GET':
            params=parse_qs(urlsplit(self.path).query)
            try:
                asset=int(params['maskedDefId'][0]) if 'maskedDefId' in params else None
                start=int(params.get('start',['0'])[0])
                count=int(params.get('num',['11'])[0])
                listings=runtime.club.market_listings(asset,start,count,filters=params)
                # A lone listing has no comparable price. Keep the query exact
                # to the card edition; never synthesize other editions here.
                if any(key.lower() in ('definitionid','defid') for key in params) and len(runtime.club.market_listings(
                        asset,0,2,filters=params))<2:
                    listings=[]
            except (ValueError,StateError,IndexError,TypeError) as exc:
                self.send(400,{'error':'INVALID_MARKET_QUERY','reason':str(exc)});return
            runtime.event('market_search',assetId=asset,query=params,results=len(listings))
            live=runtime.club.transfer_counts()['liveTransfers']
            self.send(200,{'auctionInfo':[runtime.market_auction_wire(row) for row in listings],
                           'credits':runtime.club.status()['credits'],
                           'count':len(listings),'total':live})
            return
        if path=='/ut/game/fifa22/marketdata/pricelimits' and self.command=='GET':
            params=parse_qs(urlsplit(self.path).query)
            ids=[]
            for value in params.get('defId',[]):
                for part in value.split(','):
                    if part.isdigit():ids.append(int(part))
            catalogue=getattr(runtime,'_catalogue_cache',None)
            if catalogue is None:
                catalogue=load_catalogue();runtime._catalogue_cache=catalogue
            by_resource={int(row['resourceId']):row for row in catalogue['players']}
            by_asset={int(row['assetId']):row for row in catalogue['players']
                      if not row.get('special')}
            limits=[]
            for resource_id in (ids or [0]):
                card=(by_resource.get(original_id(resource_id))
                      or by_asset.get(resource_id))
                minimum,maximum=price_bounds(card or {
                    'resourceId':resource_id,'assetId':resource_id,
                    'rating':75,'rareflag':1})
                limits.append({'itemId':resource_id,'defId':resource_id,
                               'minPrice':minimum,'maxPrice':maximum})
            self.send(200,limits)
            return
        if path=='/ut/game/fifa22/auctionhouse/relist' and self.command=='PUT':
            runtime.club.relist_expired()
            self.send(200,{'dynamicObjectivesUpdates':[]});return
        if path=='/ut/game/fifa22/auctionhouse' and self.command=='POST':
            try:
                payload=json.loads(body or b'{}')
                item=payload.get('itemData') or {}
                trade_id=runtime.club.list_for_sale(
                    int(item['id']),int(payload['startingBid']),
                    int(payload['buyNowPrice']),int(payload.get('duration',3600)))
            except (ValueError,KeyError,TypeError,StateError,AttributeError) as exc:
                runtime.event('auction_listing_failed',reason=str(exc)[:120])
                self.send(409,{'error':'LISTING_FAILED','reason':str(exc)[:120]});return
            runtime.event('auction_listed',tradeId=trade_id,itemId=int(item['id']))
            self.send(200,{'id':trade_id})
            return
        offer=re.fullmatch(r'/ut/game/fifa22/trade/(\d+)/offer',path)
        if offer and self.command=='POST':
            try:
                payload=json.loads(body or b'{}')
                record,credits=runtime.club.market_offer(int(offer[1]),int(payload['bid']))
            except (ValueError,KeyError,TypeError,StateError) as exc:
                self.send(409,{'error':'MARKET_OFFER_FAILED','reason':str(exc)});return
            runtime.event('market_offer',tradeId=record['tradeId'],
                          state=record['state'],credits=credits)
            self.send(200,{'auctionInfo':[runtime.market_auction_wire(record)],
                           'credits':credits,'duplicateItemIdList':runtime.purchased_items()['duplicateItemIdList']})
            return
        if path=='/ut/game/fifa22/trade/status/lite' and self.command=='GET':
            params=parse_qs(urlsplit(self.path).query)
            try:
                ids={int(x) for raw in params.get('tradeIds',[]) for x in raw.split(',') if x}
            except ValueError:
                self.send(400,{'error':'INVALID_TRADE_IDS'});return
            records=runtime.club.market_trades(ids)
            owned=runtime.club.transfer_list(ids)
            self.send(200,{'auctionInfo':([runtime.market_auction_wire(row) for row in records]
                                           +[runtime.transfer_auction_wire(row) for row in owned]),
                           'credits':runtime.club.status()['credits']})
            return
        if path in ('/ut/game/fifa22/watchList','/ut/delete/game/fifa22/watchList','/ut/delete/fifa22/watchList') and self.command in ('PUT','POST','DELETE'):
            try:
                payload=json.loads(body or b'{}')
                params=parse_qs(urlsplit(self.path).query)
                ids={int(x['id'] if isinstance(x,dict) else x) for x in payload.get('auctionInfo',[])}
                ids.update(int(x) for raw in params.get('tradeId',params.get('tradeIds',[])) for x in raw.split(',') if x)
                runtime.club.watch_trade(ids,self.command!='DELETE' and '/delete/' not in path)
            except (ValueError,TypeError,KeyError):
                self.send(400,{'error':'INVALID_TRADE_IDS'});return
            self.send(200,{});return
        remove=re.fullmatch(r'/ut/(?:delete/)?(?:game/)?fifa22/trade/(sold|[0-9]+)',path)
        if remove and (self.command=='DELETE' or '/delete/' in path and self.command=='POST'):
            runtime.club.clear_sold(None if remove[1]=='sold' else int(remove[1]))
            self.send(200,{});return
        if path=='/ut/game/fifa22/watchList' and self.command=='GET':
            records=runtime.club.market_trades(bids_only=True)
            self.send(200,{'auctionInfo':[runtime.market_auction_wire(row) for row in records],
                           'credits':runtime.club.status()['credits']})
            return
        if path=='/ut/game/fifa22/tradePile' and self.command=='GET':
            self.send(200,{'auctionInfo':[runtime.transfer_auction_wire(row)
                                           for row in runtime.club.transfer_list()],
                           'credits':runtime.club.status()['credits']})
            return
        if path=='/ut/game/fifa22/user/credits' and self.command=='GET':
            self.send(200,runtime.credits_payload())
            return
        if path=='/ut/delete/auth' and self.command=='POST':
            self.send(200,{})
            return
        # Native FIFA 22 Squad Building Challenges. The workspace is isolated
        # from the active XI; only an explicit validated submit consumes cards.
        if path in ('/ut/game/fifa22/sbs/sets',
                    '/ut/game/fifa22/clientdata/squadbuildingsets') and self.command=='GET':
            self.send(200,runtime.sbc_sets_payload(),extra_headers={
                'Cache-Control':'no-store, no-cache, must-revalidate',
                'Pragma':'no-cache'});return
        sbc_set=re.fullmatch(r'/ut/game/fifa22/sbs/setid/(\d+)/challenges',path,re.I)
        if sbc_set and self.command=='GET':
            entry=SET_BY_ID.get(int(sbc_set[1]))
            if entry is None:
                self.send(404,{'error':'SBC_SET_NOT_FOUND'});return
            self.send(200,{'challenges':[runtime.sbc_challenge_payload(
                entry,row,index) for index,row in enumerate(entry['challenges'])]},
                extra_headers={'Cache-Control':'no-store, no-cache, must-revalidate',
                               'Pragma':'no-cache'})
            return
        sbc_squad=re.fullmatch(r'/ut/game/fifa22/sbs/challenge/(\d+)/squad',path,re.I)
        if sbc_squad:
            challenge_id=int(sbc_squad[1]);catalog=CHALLENGE_BY_ID.get(challenge_id)
            if catalog is None:
                self.send(404,{'error':'SBC_CHALLENGE_NOT_FOUND'});return
            set_entry,challenge=catalog
            if self.command in ('POST','PUT') and body:
                try:
                    document=json.loads(body)
                    runtime.club.save_sbc_squad(challenge_id,document,
                                                int(challenge['players']))
                except (ValueError,UnicodeDecodeError,StateError) as exc:
                    self.send(409,{'error':'SBC_SQUAD_SAVE_FAILED','reason':str(exc)});return
            response=runtime.sbc_squad_response(set_entry,challenge)
            response['saved']=self.command in ('POST','PUT')
            self.send(200,response);return
        sbc_validate=re.fullmatch(r'/ut/game/fifa22/sbs/challenge/(\d+)/validate',path,re.I)
        if sbc_validate and self.command in ('GET','POST','PUT'):
            challenge_id=int(sbc_validate[1]);catalog=CHALLENGE_BY_ID.get(challenge_id)
            if catalog is None:
                self.send(404,{'error':'SBC_CHALLENGE_NOT_FOUND'});return
            _,challenge=catalog
            try:
                if body:
                    document=json.loads(body)
                    if runtime.club._sbc_item_ids(document):
                        runtime.club.save_sbc_squad(challenge_id,document,
                                                    int(challenge['players']))
                valid,checks,_=runtime.sbc_evaluate(
                    challenge,runtime.club.sbc_squad(challenge_id))
            except (ValueError,UnicodeDecodeError,StateError) as exc:
                self.send(409,{'error':'SBC_VALIDATE_FAILED','reason':str(exc)});return
            self.send(200,{'challengeId':challenge_id,'valid':valid,
                           'meetsRequirements':valid,'requirements':checks});return
        sbc_submit=re.fullmatch(r'/ut/game/fifa22/sbs/challenge/(\d+)(/submit)?',path,re.I)
        if sbc_submit:
            challenge_id=int(sbc_submit[1]);catalog=CHALLENGE_BY_ID.get(challenge_id)
            if catalog is None:
                self.send(404,{'error':'SBC_CHALLENGE_NOT_FOUND'});return
            set_entry,challenge=catalog
            if self.command=='GET':
                self.send(200,runtime.sbc_squad_response(set_entry,challenge));return
            if self.command in ('POST','PUT'):
                try:document=json.loads(body) if body else {}
                except (ValueError,UnicodeDecodeError):document={}
                params=parse_qs(urlsplit(self.path).query)
                incoming=bool(runtime.club._sbc_item_ids(document))
                explicit=bool(sbc_submit[2]) or self.command=='PUT' or incoming or any(
                    str(key).lower()=='skipusersquadvalidation' for key in params)
                if not explicit:
                    self.send(200,runtime.sbc_squad_response(set_entry,challenge));return
                try:
                    if incoming:
                        runtime.club.save_sbc_squad(challenge_id,document,
                                                    int(challenge['players']))
                    saved=runtime.club.sbc_squad(challenge_id)
                    valid,checks,item_ids=runtime.sbc_evaluate(challenge,saved)
                    if not valid:
                        self.send(200,{'success':False,'challengeComplete':False,
                                       'meetsRequirements':False,'challengeId':challenge_id,
                                       'setId':int(set_entry['setId']),'validation':checks,
                                       'error':{'code':'SBC_REQUIREMENTS_NOT_MET'}});return
                    result=runtime.club.complete_sbc(
                        challenge_id,int(set_entry['setId']),item_ids,
                        list(challenge.get('awards',[])),
                        [int(row['challengeId']) for row in set_entry['challenges']],
                        list(set_entry.get('awards',[])),
                        repeatable=bool(set_entry.get('repeatable',False)))
                except StateError as exc:
                    self.send(409,{'error':'SBC_SUBMISSION_CONFLICT','reason':str(exc)});return
                awards=runtime._sbc_awards(result.pop('awards',[]))
                response={'success':True,'challengeComplete':True,
                          'meetsRequirements':True,'challengeId':challenge_id,
                          'setId':int(set_entry['setId']),'validation':checks,
                          'awards':awards,'rewards':awards,**result,
                          **runtime.credits_payload()}
                runtime.event('sbc_complete',challengeId=challenge_id,
                              setId=int(set_entry['setId']),consumed=len(item_ids),
                              setCompleted=bool(response.get('setCompleted')))
                self.send(200,response);return
        if path=='/ut/game/fifa22/sku/FFA22PCC/store/category' and self.command=='GET':
            # RS4:FutStoreGetCategoriesServerResponse: one key, an array of
            # {categoryId, localizedName, order}. This press opens the store.
            self.send(200,runtime.store_categories(),extra_headers={
                'Cache-Control':'no-store, no-cache, must-revalidate',
                'Pragma':'no-cache'})
            return
        if path=='/ut/game/fifa22/store/category' and self.command=='GET':
            self.send(200,runtime.store_categories(),extra_headers={
                'Cache-Control':'no-store, no-cache, must-revalidate',
                'Pragma':'no-cache'})
            return
        if path.startswith('/ut/game/fifa22/store') and self.command=='GET':
            # /store/purchasegroup/all is THE pack list (RS4:FutStoreGetPackTypes
            # ServerResponse). The rows carry the availability, tile-image and
            # post-purchase fields the client filters on; see _store_pack_row.
            self.send(200,runtime.store_pack_list(),extra_headers={
                'Cache-Control':'no-store, no-cache, must-revalidate',
                'Pragma':'no-cache'})
            return
        def store_buy(pack_id):
            """Charge for a pack and deliver the previewed cards."""
            # The preview, if one was shown, IS the pack: deliver exactly the
            # cards the client was shown (fifa22work contract).
            preview=getattr(runtime,'_preview_rolls',{}).pop(pack_id,None)
            operation_ids=getattr(runtime,'_preview_operation_ids',{})
            operation_id=operation_ids.pop(pack_id,None)
            recent=getattr(runtime,'_recent_pack_operations',None)
            if recent is None:
                recent={};runtime._recent_pack_operations=recent
            # A ProtoHttp retry arrives without another preview. Replay the
            # just-completed operation briefly, but never merge two deliberate
            # purchases of the same pack into one ten-minute bucket.
            if operation_id is None:
                last=recent.get(pack_id)
                if last and time.time()-last[1]<5:
                    self.send(200,last[2]);return
                else:
                    operation_id=f'direct:{pack_id}:{secrets.token_hex(12)}'
            pack=next((row for row in STORE_PACKS if row['id']==pack_id),None)
            if pack is None:
                self.send(404,{'error':'UNKNOWN_PACK'});return
            if pack.get('playerPick'):
                runtime.clear_walkout_request('player pick')
                try:
                    result=runtime.purchase_player_pick(pack,operation_id)
                except Exception as exc:
                    runtime.event('player_pick_grant_failed',packId=pack_id,
                                  error=str(exc))
                    self.send(409,{'error':'PLAYER_PICK_PURCHASE_FAILED',
                                   'reason':str(exc)[:120]});return
                ordered=[runtime.item_wire(item) for item in result['itemData']]
                runtime.push_player_pick('store purchase')
                token_id=int(ordered[0]['id'])
                response={'numberItems':1,'purchasedPackId':pack_id,
                          'packId':pack_id,'duplicateItemIdList':[],
                          'itemList':ordered,'itemData':ordered,'items':ordered,
                          'playerPickItemId':token_id,'playerPickId':token_id,
                          'isPlayerPick':True,
                          'credits':result['credits'],'coins':result['credits'],
                          'currencies':[{'name':'COINS','funds':result['credits'],
                                         'finalFunds':result['credits']},
                                        {'name':'POINTS','funds':0,'finalFunds':0}],
                          'skipPackOpeningAnimation':True,
                          'packOpeningAnimationEnabled':False,'success':True}
                recent[pack_id]=(operation_id,time.time(),response)
                runtime.event('pack_purchase',packId=pack_id,items=1,
                              playerPick=True,fromPreview=False,reward=False,
                              animation=False,credits=result['credits'])
                runtime.record_objective_pack(pack,1,operation_id)
                self.send(200,response);return
            reward=runtime.club.reward_pack_count(pack_id)>0
            try:
                result=runtime.club.purchase(pack_id,operation_id,
                                             force_ids=preview,reward=reward)
            except Exception as exc:
                runtime.event('pack_purchase_failed',packId=pack_id,error=str(exc))
                self.send(409,{'error':'PACK_PURCHASE_FAILED',
                               'reason':str(exc)[:120]})
                return
            ordered=[runtime.item_wire(item) for item in result['itemData']]
            ordered.sort(key=runtime._pack_reveal_key)
            if not pack.get('skipAnimation'):
                runtime.publish_walkout_request(pack_id,ordered)
            else:
                runtime.clear_walkout_request('pack without animation')
            runtime.event('pack_purchase',packId=pack_id,
                          items=len(result['itemData']),playerPick=False,
                          fromPreview=bool(preview),
                          reward=bool(result.get('rewardPack')),
                          animation=not bool(pack.get('skipAnimation')),
                          credits=result['credits'])
            runtime.record_objective_pack(pack,len(result['itemData']),operation_id)
            # Keep FIFA 22's native purchased-items aliases in sync. The New
            # Items screen refetches this pile immediately after the ceremony.
            response={'numberItems':len(ordered),
                      'purchasedPackId':pack_id,'packId':pack_id,
                      'duplicateItemIdList':result['duplicateItemIdList'],
                      'itemList':ordered,'itemData':ordered,'items':ordered,
                      'credits':result['credits'],'coins':result['credits'],
                      'currencies':[{'name':'COINS','funds':result['credits'],
                                     'finalFunds':result['credits']},
                                    {'name':'POINTS','funds':0,'finalFunds':0}],
                      'skipPackOpeningAnimation':bool(pack.get('skipAnimation')),
                      'packOpeningAnimationEnabled':not bool(pack.get('skipAnimation')),
                      'success':True}
            recent[pack_id]=(operation_id,time.time(),response)
            self.send(200,response)

        pack_preview=re.fullmatch(
            r'/ut/game/fifa22/preview/pack/(\d+)/(items|duplicates)',path)
        if pack_preview:
            pack_id=int(pack_preview[1])
            if pack_preview[2]=='duplicates':
                self.send(200,{'duplicateItemIdList':[]});return
            if self.command in ('POST','PUT'):
                store_buy(pack_id);return
            if self.command=='GET':
                roll=runtime.preview_roll(pack_id,refresh=self.command=='GET')
                if roll is None:
                    self.send(200,{'itemList':[],'numberItems':0,'packId':pack_id})
                    return
                cards=[]
                for n,row in enumerate(roll):
                    item_id=9000000+pack_id*100+n
                    if str(row.get('itemType','player')).lower()=='player':
                        cards.append(runtime._sqbt_catalogue_player_card(row,item_id))
                    else:
                        cards.append(runtime.item_wire(dict(row,id=item_id,pile=7)))
                cards.sort(key=lambda c:int(c.get('rating') or 0),reverse=True)
                runtime.event('pack_preview',packId=pack_id,items=len(cards),
                              ratings=[int(c.get('rating') or 0) for c in cards])
                self.send(200,{'itemList':cards,'numberItems':len(cards),
                               'packId':pack_id})
                return
        if path in ('/ut/game/fifa22/purchased/items','/ut/game/fifa22/purchase',
                    '/ut/game/fifa22/purchased') and self.command=='GET':
            self.send(200,runtime.purchased_items())
            return
        if path in ('/ut/game/fifa22/purchased/items','/ut/game/fifa22/purchase',
                    '/ut/game/fifa22/purchased') and self.command in ('POST','PUT'):
            try:payload=json.loads(body) if body else {}
            except (ValueError,UnicodeDecodeError):
                self.send(400,{'error':'INVALID_PURCHASE'});return
            pack_id=runtime._sqbt_find_int(payload,'packId','packid','id',default=0)
            if not pack_id:
                tail=path.rstrip('/').rsplit('/',1)[-1]
                pack_id=int(tail) if tail.isdigit() else 0
            if not pack_id:
                self.send(400,{'error':'PACK_ID_REQUIRED'});return
            store_buy(pack_id);return
        if path=='/ut/game/fifa22/item/nontargeted' and self.command in ('POST','PUT'):
            params=parse_qs(urlsplit(self.path).query);payload={}
            if body:
                try:payload=json.loads(body)
                except (ValueError,UnicodeDecodeError):
                    try:payload={key:value[-1] for key,value in
                                 parse_qs(body.decode('utf-8')).items()}
                    except UnicodeDecodeError:payload={}
            item_id=runtime._sqbt_find_int_nested(
                payload,'itemId','itemid','id','playerPickItemId',default=0)
            if not item_id:
                item_id=runtime._sqbt_find_int(
                    {key:value[-1] for key,value in params.items()},
                    'itemId','itemid','id','playerPickItemId',default=0)
            pending=runtime.club.pending_player_picks()
            if not pending:
                # FIFA can repeat the redeem request after the previous choice
                # has already been committed.  Treat that retry as an empty,
                # successful queue instead of returning 409 and crashing the
                # next Player Pick transition.
                self.send(200,runtime.player_pick_payload(),
                          extra_headers={'Cache-Control':'no-store'});return
            requested=next((row for row in pending
                            if runtime.club.player_pick_placeholder_id(row['id'])==item_id),
                           None)
            pick=requested or next((row for row in pending if row.get('opened')),
                                   pending[0])
            token_id=runtime.club.player_pick_placeholder_id(pick['id'])
            try:
                runtime.club.open_player_pick(token_id)
                runtime.push_player_pick('redeem')
            except StateError as exc:
                self.send(409,{'error':'PLAYER_PICK_OPEN_FAILED',
                               'reason':str(exc)});return
            response=runtime.player_pick_payload()
            runtime.event('player_pick_opened',pickId=token_id,
                          choices=response['numberItems'])
            self.send(200,response,extra_headers={'Cache-Control':'no-store'});return
        pick_open=re.fullmatch(r'/ut/game/fifa22/item/(\d+)',path)
        if pick_open and self.command in ('POST','PUT'):
            item_id=int(pick_open[1])
            pick=next((row for row in runtime.club.pending_player_picks()
                       if runtime.club.player_pick_placeholder_id(row['id'])==item_id),None)
            if pick:
                try:
                    runtime.club.open_player_pick(item_id)
                    runtime.push_player_pick('redeem')
                except StateError as exc:
                    self.send(409,{'error':'PLAYER_PICK_OPEN_FAILED',
                                   'reason':str(exc)});return
                response=runtime.player_pick_payload()
                runtime.event('player_pick_opened',pickId=pick['id'],
                              choices=response['numberItems'])
                self.send(200,response,extra_headers={'Cache-Control':'no-store'});return
        if pick_open and self.command=='POST':
            # Pack-result redemption also uses the instance URL for ordinary
            # items.  Returning the generic ``{}`` fallback leaves the New
            # Items controller waiting forever.  Acknowledge the concrete item
            # document; pile movement remains on the dedicated /item endpoint.
            item_id=int(pick_open[1])
            stored=next((row for row in runtime.club.list_items()
                         if int(row.get('id',0))==item_id),None)
            if stored is None:
                self.send(404,{'error':'ITEM_NOT_FOUND','id':item_id});return
            wire=runtime.item_wire(stored)
            self.send(200,{'itemData':[wire],'items':[wire],'success':True},
                      extra_headers={'Cache-Control':'no-store'});return
        consumable_apply=re.fullmatch(r'/ut/game/fifa22/item/resource/(\d+)',path)
        if consumable_apply and self.command in ('POST','PUT'):
            try:payload=json.loads(body or b'{}')
            except (ValueError,UnicodeDecodeError):payload={}
            target_ids=[]
            def collect(value):
                if isinstance(value,list):
                    for row in value:collect(row)
                elif isinstance(value,dict):
                    direct=value.get('id') or value.get('itemId') or value.get('itemid')
                    if str(direct).isdigit() and int(direct) not in target_ids:
                        target_ids.append(int(direct))
                    else:
                        for row in value.values():collect(row)
                elif str(value).isdigit() and int(value) not in target_ids:
                    target_ids.append(int(value))
            # These are the native FIFA application containers. Restricting
            # the first pass prevents resourceId/count fields from being
            # mistaken for player item IDs and producing an empty reply.
            if isinstance(payload,dict):
                for key in ('apply','itemData','items','targets'):
                    if key in payload:collect(payload[key])
                if not target_ids:collect(payload)
            elif isinstance(payload,list):collect(payload)
            updated=runtime.apply_consumable(int(consumable_apply[1]),target_ids)
            runtime.event('consumable_apply',resourceId=int(consumable_apply[1]),
                          targets=target_ids,updated=[row['id'] for row in updated])
            self.send(200,{'itemData':updated,'items':updated,
                           'consumableConsumed':bool(updated)},
                      extra_headers={'Cache-Control':'no-store'})
            return
        item_view=re.fullmatch(r'/ut/game/fifa22/item(?:/([0-9,]+))?',path)
        if item_view and item_view[1] and self.command=='PUT' and ',' not in item_view[1]:
            try:
                payload=json.loads(body or b'{}')
                item_id=int(item_view[1])
                stored=next((row for row in runtime.club.list_items('club')
                             if int(row['id'])==item_id),None)
                if stored is None:raise StateError('Club item is not owned')
                requested=str(payload.get('itemState',payload.get('state','')))
                slot=runtime._sqbt_find_int(payload,'activateSlotNumber','slot',default=-1)
                if requested in ('','active','free'):
                    requested={0:'activeHomeKit',1:'activeAwayKit',
                               2:'activeBadge',3:'activeBadge',4:'activeBall',
                               5:'activeStadium',101:'activeHomeKit',
                               102:'activeAwayKit'}.get(slot,'')
                updated=runtime.club.activate_club_item(item_id,requested)
            except (ValueError,TypeError,StateError,UnicodeDecodeError) as exc:
                self.send(409,{'error':'CLUB_ITEM_ACTIVATION_FAILED',
                               'reason':str(exc)});return
            runtime.event('club_item_activated',itemId=item_id,
                          resourceId=updated.get('resourceId'),state=requested)
            self.send(200,b'',content_type='application/json');return
        if item_view and self.command=='GET':
            params=parse_qs(urlsplit(self.path).query)
            ids=[]
            chunks=[]
            if item_view[1]:chunks.append(item_view[1])
            chunks.extend(params.get('idList',params.get('idlist',[])))
            for chunk in chunks:
                ids.extend(int(value) for value in str(chunk).split(',')
                           if value.strip().isdigit())
            all_items={int(item['id']):item for pile in ('club','unassigned','trade')
                       for item in runtime.club.list_items(pile)}
            self.send(200,{'itemData':[runtime.item_wire(all_items[item_id])
                                       for item_id in ids if item_id in all_items]})
            return
        discard_item=re.fullmatch(r'/ut/(?:delete/)?game/fifa22/item(?:/([0-9,]+))?',path)
        if discard_item and (self.command=='DELETE' or '/delete/' in path):
            try:
                payload=json.loads(body or b'{}')
                raw=payload.get('itemId',payload.get('itemData',[])) if isinstance(payload,dict) else payload
                if isinstance(raw,(int,str,dict)):raw=[raw]
                ids=[]
                for row in (raw or []):
                    value=row.get('id',row.get('itemId')) if isinstance(row,dict) else row
                    if str(value).isdigit():ids.append(int(value))
                if discard_item[1]:
                    ids.extend(int(value) for value in discard_item[1].split(',')
                               if value.isdigit())
                result=runtime.club.discard_items(ids)
            except (ValueError,TypeError,StateError,AttributeError) as exc:
                self.send(409,{'error':'ITEM_DISCARD_FAILED','reason':str(exc)});return
            runtime.event('item_discard',itemIds=result['discarded'],
                          value=result['value'],credits=result['credits'])
            credits=int(result['credits'])
            # FutDiscardItemsResponse requires items[] acknowledgements. A
            # scalar count leaves FIFA waiting forever on "Updating Items".
            self.send(200,{'id':result['discarded'],
                           'items':[{'id':item_id}
                                    for item_id in result['discarded']],
                           'itemData':[{'id':item_id}
                                       for item_id in result['discarded']],
                           'totalCredits':credits,'credits':credits,
                           'currencies':[{'name':'coins','funds':credits,
                                          'finalFunds':credits}]})
            return
        if path=='/ut/game/fifa22/item' and self.command in ('POST','PUT'):
            try:
                payload=json.loads(body or b'{}')
                if not isinstance(payload,dict):raise StateError('Invalid move payload')
                raw=payload.get('itemData',payload.get('items',payload.get('itemId',[])))
                if isinstance(raw,(int,str,dict)):raw=[raw]
                if not isinstance(raw,list) or not raw:
                    raise StateError('No items supplied')

                # Native FIFA sends the destination on each itemData entry,
                # e.g. {"itemData":[{"id":123,"pile":"trade"}]}.  The old
                # handler only inspected payload.pile, silently defaulted such
                # requests to Club, and duplicate repair consequently returned
                # the cards to New Items after a restart.  Numeric pile 5 is
                # the Transfer List and 6 is the Club in FIFA 22's wire model.
                def move_pile(value):
                    target=str(value).strip().lower()
                    aliases={
                        '5':'trade','trade':'trade','transfer':'trade',
                        'transferlist':'trade','transfer_list':'trade',
                        '6':'club','club':'club','squad':'club',
                        '0':'unassigned','1':'unassigned',
                        'unassigned':'unassigned','new':'unassigned',
                        'newitems':'unassigned','new_items':'unassigned',
                    }
                    if target not in aliases:raise StateError('Invalid pile')
                    return aliases[target]

                top_pile=None
                for key in ('pile','targetPile','pileId'):
                    if key in payload and payload[key] is not None:
                        top_pile=payload[key];break
                moves=[]
                for row in raw:
                    if isinstance(row,dict):
                        value=row.get('id',row.get('itemId'))
                        row_pile=None
                        for key in ('pile','targetPile','pileId'):
                            if key in row and row[key] is not None:
                                row_pile=row[key];break
                    else:
                        value=row;row_pile=None
                    item_id=int(value)
                    target=move_pile(row_pile if row_pile is not None
                                     else (top_pile if top_pile is not None
                                           else 'club'))
                    moves.append((item_id,target))
                if len({item_id for item_id,_ in moves})!=len(moves):
                    raise StateError('Duplicate item IDs')
                for item_id,target in moves:
                    runtime.club.move([item_id],target)
            except (ValueError,TypeError,KeyError,StateError,AttributeError) as exc:
                self.send(409,{'error':'ITEM_MOVE_FAILED','reason':str(exc)});return
            # FIFA 22's move response consumes one acknowledgement per item.
            # An empty object makes it retry the PUT indefinitely, even after
            # the database has already moved the card.
            runtime.event('item_move',itemIds=[item_id for item_id,_ in moves],
                          piles={str(item_id):target for item_id,target in moves})
            wire_piles={'unassigned':1,'trade':5,'club':6}
            self.send(200,{'itemData':[{'id':item_id,'pile':wire_piles[target],
                                        'success':True}
                                       for item_id,target in moves]})
            return
        if re.fullmatch(r'/fut/packs/loc/storepackdescriptions\.[a-z]{2}_[a-z]{2}\.xml',path) and self.command=='GET':
            lines=['<?xml version="1.0" encoding="UTF-8"?>',
                   '<xliff version="1.2">',
                   '  <file original="storepackdescriptions" source-language="en-US" datatype="plaintext">',
                   '    <body>']
            for pack in STORE_PACKS:
                for key,text_value in (
                        (f'FUT_STORE_PACK_{pack["id"]}_NAME',pack['name']),
                        (f'FUT_STORE_PACK_{pack["id"]}_DESC',pack['description']),
                        (f'FUT_STORE_PACK_{pack["id"]}_NAME_MOBILE',pack['name'])):
                    lines.append(f'      <trans-unit id="{key}" resname="{key}">'
                                 f'<source>{text_value}</source></trans-unit>')
            for rarity,label in sorted(WALKOUT_RARITY_LABELS.items()):
                key=f'FUT_POA_PLAYER_RARITY_{rarity}'
                lines.append(f'      <trans-unit id="{key}" resname="{key}">'
                             f'<source>{label}</source></trans-unit>')
            lines.extend(['    </body>','  </file>','</xliff>'])
            self.send(200,'\n'.join(lines).encode('utf-8'),
                      'application/xml; charset=utf-8')
            return
        offline_squad=re.fullmatch(r'/ut/game/fifa22/offlinesquad/(\d+)',path)
        if offline_squad and self.command=='GET':
            # Player squad for the user's id; CPU squad when the id is one we
            # published on /sqbt/user/opponentsquad.
            wanted=int(offline_squad[1])
            draft=runtime._draft_load_state()
            if wanted==runtime.DRAFT_SQUAD_ID and draft:
                squad=runtime.draft_squad(draft)
            elif (runtime.DRAFT_OPPONENT_SQUAD_ID<=wanted
                  <runtime.DRAFT_OPPONENT_SQUAD_ID+4):
                squad=runtime.draft_opponent_squad(
                    wanted-runtime.DRAFT_OPPONENT_SQUAD_ID)
                if squad is None:squad={}
            elif SQBT_SQUAD_ID<=wanted<SQBT_SQUAD_ID+10:
                squad=runtime.squad_battles_opponent_squad(wanted-SQBT_SQUAD_ID)
                if squad is None:squad={}
            elif wanted in runtime.club.squad_ids():
                squad=runtime.regular_squad(wanted)
            else:
                squad=dict(runtime.user_mass_info()['squad'])
                squad['id']=wanted
                squad['squadId']=wanted
            # Port the proven FIFA 20 Draft fix to the updated FIFA 22 Draft:
            # the match frontend loads the CPU team from this separate route,
            # so the selected difficulty has to travel on this document too.
            if (draft and squad and
                    (wanted==runtime.DRAFT_SQUAD_ID or
                     runtime.DRAFT_OPPONENT_SQUAD_ID<=wanted
                     <runtime.DRAFT_OPPONENT_SQUAD_ID+4)):
                difficulty=max(1,min(6,int(draft.get('difficulty',3) or 3)))
                difficulty_name=next((name for value,name,_ in
                                      runtime.DRAFT_DIFFICULTIES
                                      if value==difficulty),'PRO')
                squad.update(matchDifficulty=difficulty,
                             sqbtMatchDifficulty=difficulty,
                             difficulty=difficulty,difficultyId=difficulty,
                             gameDifficulty=difficulty,
                             difficultyName=('PROFESSIONAL'
                                             if difficulty_name=='PRO'
                                             else difficulty_name))
            runtime.event('offline_squad',squadId=wanted,
                          personaId=int(squad.get('personaId',-1) if squad else -1))
            self.send(200,squad or {})
            return
        if path in ('/ut/game/fifa22/squad/mode/draft/opponent',
                    '/ut/game/fifa22/draft/mode/draft/opponent') and self.command=='GET':
            draft=runtime._draft_load_state() or {}
            squad=runtime.draft_opponent_squad(int(draft.get('wins',0) or 0))
            if squad and draft:
                difficulty=max(1,min(6,int(draft.get('difficulty',3) or 3)))
                difficulty_name=next((name for value,name,_ in
                                      runtime.DRAFT_DIFFICULTIES
                                      if value==difficulty),'PRO')
                squad.update(matchDifficulty=difficulty,
                             sqbtMatchDifficulty=difficulty,
                             difficulty=difficulty,difficultyId=difficulty,
                             gameDifficulty=difficulty,
                             difficultyName=('PROFESSIONAL'
                                             if difficulty_name=='PRO'
                                             else difficulty_name))
            self.send(200,squad or {})
            return
        if path in ('/ut/game/fifa22/squad/list','/ut/game/fifa22/squad') \
                and self.command=='GET':
            self.send(200,runtime.squad_list_payload(),
                      extra_headers={'Cache-Control':'no-store'})
            return
        if path=='/ut/game/fifa22/squad' and self.command in ('POST','PUT'):
            try:payload=json.loads(body or b'{}')
            except (ValueError,UnicodeDecodeError):
                self.send(400,{'error':'INVALID_SQUAD_BODY'});return
            try:
                # POST is always a create. FIFA includes the selected squad id
                # in its template; treating it as an update overwrites squad 1.
                has_id=isinstance(payload,dict) and bool(
                    payload.get('id') or payload.get('squadId'))
                saved=runtime.save_regular_squad(
                    payload,create=self.command=='POST' or not has_id)
            except (StateError,TypeError,ValueError) as exc:
                self.send(409,{'error':'INVALID_SQUAD','reason':str(exc)[:160]});return
            response=dict(saved,success=True,squad=saved)
            self.send(200,response,extra_headers={'Cache-Control':'no-store'});return
        if path=='/ut/game/fifa22/squad/active':
            if self.command in ('POST','PUT'):
                try:payload=json.loads(body or b'{}')
                except (ValueError,UnicodeDecodeError):payload={}
                try:wanted=int(payload.get('id') or payload.get('squadId') or 0)
                except (TypeError,ValueError,AttributeError):wanted=0
                if wanted<=0 or not runtime.club.set_active_squad(wanted):
                    self.send(404,{'error':'SQUAD_NOT_FOUND','id':wanted});return
                runtime.event('squad_selected',squadId=wanted,source='active')
            if self.command in ('GET','POST','PUT'):
                self.send(200,runtime.regular_squad(),
                          extra_headers={'Cache-Control':'no-store'})
                return
        squad_clone=re.fullmatch(r'/ut/game/fifa22/squad(?:/(\d+))?/clone',path)
        if squad_clone and self.command in ('POST','PUT'):
            try:payload=json.loads(body or b'{}')
            except (ValueError,UnicodeDecodeError):payload={}
            try:
                source_id=int(squad_clone[1] or payload.get('srcId') or
                              payload.get('sourceSquadId') or 0)
            except (TypeError,ValueError,AttributeError):source_id=0
            if source_id not in runtime.club.squad_ids():
                self.send(404,{'error':'SQUAD_NOT_FOUND','id':source_id});return
            source=runtime.regular_squad(source_id)
            clone=dict(source)
            clone.pop('id',None);clone.pop('squadId',None);clone.pop('active',None)
            if isinstance(payload,dict):
                clone['squadName']=str(payload.get('dstSquadName') or
                                       payload.get('squadName') or
                                       payload.get('name') or
                                       f'{source.get("squadName","Squad")} Copy')
                clone['name']=clone['squadName']
                if payload.get('formation'):clone['formation']=payload['formation']
            try:saved=runtime.save_regular_squad(clone,create=True)
            except (StateError,TypeError,ValueError) as exc:
                self.send(409,{'error':'INVALID_SQUAD','reason':str(exc)[:160]});return
            self.send(200,dict(saved,success=True,squad=saved),
                      extra_headers={'Cache-Control':'no-store'});return
        squad_save=re.fullmatch(r'/ut/game/fifa22/squad/(\d+)',path)
        if squad_save and self.command=='GET':
            squad_id=int(squad_save[1])
            if squad_id==runtime.DRAFT_SQUAD_ID:
                state=runtime._draft_load_state()
                if not state or state.get('draftState') in ('NOT_STARTED','',None):
                    self.send(404,{'error':'DRAFT_SQUAD_NOT_FOUND','id':squad_id});return
                self.send(200,runtime.draft_squad(state),
                          extra_headers={'Cache-Control':'no-store'})
                return
            if squad_id not in runtime.club.squad_ids():
                self.send(404,{'error':'SQUAD_NOT_FOUND','id':squad_id});return
            # FIFA selects a squad by loading it and may not send a separate
            # /active request before returning to the Hub.
            runtime.club.set_active_squad(squad_id)
            runtime.event('squad_selected',squadId=squad_id,source='load')
            self.send(200,runtime.regular_squad(squad_id),
                      extra_headers={'Cache-Control':'no-store'})
            return
        if squad_save and self.command in ('POST','PUT'):
            try:payload=json.loads(body) if body else {}
            except (ValueError,UnicodeDecodeError):
                self.send(400,{'error':'INVALID_SQUAD_BODY'});return
            squad_id=int(squad_save[1])
            try:
                if squad_id==runtime.DRAFT_SQUAD_ID:
                    saved=runtime.save_draft_squad(payload)
                else:
                    saved=runtime.save_regular_squad(payload,squad_id=squad_id)
            except (StateError,TypeError,ValueError) as exc:
                self.send(409,{'error':'INVALID_SQUAD','reason':str(exc)[:160]});return
            self.send(200,dict(saved,success=True,squad=saved),
                      extra_headers={'Cache-Control':'no-store'})
            return
        if squad_save and self.command=='DELETE':
            squad_id=int(squad_save[1]);deleted=runtime.club.delete_squad(squad_id)
            self.send(200 if deleted else 409,
                      {'id':squad_id,'success':deleted},
                      extra_headers={'Cache-Control':'no-store'})
            return
        delete_squad=re.fullmatch(r'/ut/delete/(?:game/)?fifa22/squad/(\d+)',path)
        if delete_squad and self.command in ('GET','DELETE','POST','PUT'):
            squad_id=int(delete_squad[1]);deleted=runtime.club.delete_squad(squad_id)
            self.send(200 if deleted else 409,
                      {'id':squad_id,'success':deleted},
                      extra_headers={'Cache-Control':'no-store'})
            return
        # --- Pack art and other supplied FIFA 22 content ---------------------
        if path.startswith('/fut/packs/') and self.command=='GET':
            relative=path.removeprefix('/fut/')
            source=FIFA22_CONTENT/relative
            if not source.is_file() and path.startswith('/fut/packs/images/kc/'):
                source=FIFA22_CONTENT/'packs/images/background'/Path(path).name
            if source.is_file():
                raw=source.read_bytes()
                runtime.event('fifa22_content',path=path,bytes=len(raw))
                self.send(200,raw,'application/octet-stream');return
            runtime.event('fut_optional_missing',path=path)
            self.send(204,b'','application/octet-stream');return
        if path=='/ut/game/fifa22/onboarding/kits':
            listing=runtime.onboarding_kits()
            if self.command=='GET':
                self.send(200,listing)
                return
            if self.command in ('POST','PUT'):
                try:selection=json.loads(body or b'{}')
                except ValueError:
                    self.send(400,{'error':'INVALID_KIT_SELECTION'});return
                try:
                    selected_home=int(selection.get('homeKitId',listing['homeKitId']))
                    selected_away=int(selection.get('awayKitId',listing['awayKitId']))
                except (TypeError,ValueError,AttributeError):
                    self.send(400,{'error':'INVALID_KIT_SELECTION'});return
                homes={item['resourceId']:item for item in listing['homeItemDataList']}
                aways={item['resourceId']:item for item in listing['awayItemDataList']}
                if selected_home not in homes or selected_away not in aways:
                    self.send(400,{'error':'UNKNOWN_KIT'});return
                runtime.club.set_meta('homeKitId',selected_home)
                runtime.club.set_meta('awayKitId',selected_away)
                # The selected resource IDs are authoritative here. Existing
                # equipped rows still describe the previous choice and must not
                # override the just-written homeKitId/awayKitId before they are
                # demoted by ensure_cosmetics.
                resolved=runtime.club.ensure_cosmetics(runtime.worn_actives(
                    prefer_owned=False))
                chosen=[next(card for card in resolved
                             if card.get('itemState')=='activeHomeKit'),
                        next(card for card in resolved
                             if card.get('itemState')=='activeAwayKit')]
                runtime.event('kits_selected',home=selected_home,away=selected_away)
                self.send(200,{'homeKitId':selected_home,'awayKitId':selected_away,
                               'homeItemData':chosen[0],'awayItemData':chosen[1],
                               'itemData':chosen,'actives':chosen,'success':True})
                return
        if path=='/ut/game/fifa22/onboarding/badges' and self.command=='GET':
            self.send(200,runtime.onboarding_badges(),
                      extra_headers={'Cache-Control':'no-store'});return
        badge_select=re.fullmatch(r'/ut/game/fifa22/onboarding/badge/(-?\d+)',path)
        if badge_select and self.command in ('POST','PUT'):
            listing=runtime.onboarding_badges()
            requested=int(badge_select[1])
            current=int(listing['badgeId'])
            selected=next((row for row in listing['badgeItemDataList']
                           if requested in (int(row.get('id',0) or 0),
                                            int(row.get('itemId',0) or 0),
                                            int(row.get('resourceId',0) or 0),
                                            int(row.get('assetId',0) or 0),
                                            int(row.get('teamid',0) or 0))),None)
            if selected is None and requested==-1:
                selected=next((row for row in listing['badgeItemDataList']
                               if int(row.get('resourceId',0) or 0)==current),
                              listing['badgeItemDataList'][0])
            if selected is None:
                self.send(400,{'error':'UNKNOWN_BADGE','id':requested});return
            team=int(selected.get('teamid') or selected.get('teamId') or
                     selected.get('assetId') or 1)
            badge=runtime.badge_item(team,810000)
            badge.update(pile=7,itemState='activeBadge')
            runtime.club.set_meta('badgeId',int(badge['resourceId']))
            runtime.club.set_meta('onboardingComplete',1)
            actives=runtime.worn_actives(prefer_owned=False)
            actives[1]=badge
            resolved=runtime.club.ensure_cosmetics(actives)
            badge=next(card for card in resolved
                       if card.get('itemState')=='activeBadge')
            runtime.event('badge_selected',teamId=team,
                          resourceId=int(badge['resourceId']))
            self.send(200,{'badgeId':int(badge['resourceId']),
                           'selectedBadgeId':int(badge['resourceId']),
                           'badgeItemData':badge,'itemData':[badge],
                           'onboardingComplete':1,'firstTime':False,
                           'returningUser':1,'hasClub':1,'success':True},
                      extra_headers={'Cache-Control':'no-store'});return
        pick_select=re.fullmatch(
            r'/ut/game/fifa22/playerpicks?(?:/item)?/(\d+)/(?:select|selection|claim)',
            path)
        if pick_select and self.command in ('POST','PUT','DELETE'):
            try:
                selected=int(pick_select[1])
                # Several Icon editions use a FIFA-22 compatibility identity
                # on the wire. Resolve the displayed resource/asset ID back to
                # the exact definition stored in the pending pick; comparing
                # the wire alias directly with pick['choices'] rejected a
                # perfectly valid selection after it had rendered.
                pending=runtime.club.pending_player_picks()
                active=next((row for row in pending if row.get('opened')),
                            pending[0] if pending else None)
                resource_id=0
                if active:
                    definitions=runtime.club.definitions(
                        [int(value) for value in active.get('choices',[])])
                    for actual in active.get('choices',[]):
                        actual=int(actual);card=definitions.get(actual,{})
                        shown_resource=presented_id(actual)
                        shown_asset=presented_asset(
                            actual,int(card.get('assetId',actual) or actual))
                        if selected in (actual,shown_resource,shown_asset):
                            resource_id=actual;break
                if not resource_id:resource_id=original_id(selected)
                chosen=runtime.club.resolve_player_pick(resource_id)
            except StateError as exc:
                self.send(409,{'error':'PLAYER_PICK_FAILED','reason':str(exc)});return
            runtime.event('player_pick_selected',resourceId=chosen['resourceId'])
            runtime.push_player_pick('select')
            self.send(200,runtime.player_item_wire(chosen));return
        if path in ('/ut/game/fifa22/playerpicks','/ut/game/fifa22/playerpicks/pending',
                    '/ut/game/fifa22/playerpick/pending') and self.command=='GET':
            self.send(200,runtime.player_pick_payload(),
                      extra_headers={'Cache-Control':'no-store'})
            return
        if path=='/ut/game/fifa22/hub' and self.command=='GET':
            self.send(200,runtime.fut_hub(),extra_headers={'X-UT-SID':runtime.token})
            return
        if path=='/ut/game/fifa22/clubUser' and self.command=='GET':
            self.send(200,{'user':[{'persona':'LocalPlayer',
                                    'personaId':1000000022,'public':True}]})
            return
        if path=='/ut/game/fifa22/club/stats/staff' and self.command=='GET':
            # FutStaffBonusServerResponse: bonus[] of {contract, managerTalk}.
            seated=runtime.club_manager()
            self.send(200,{'bonus':[{
                'contract':int(seated.get('contract') or 7),
                'managerTalk':max(1,int(seated.get('rating') or 75)//10)}]})
            return
        if path=='/ut/game/fifa22/club/stats/consumables' and self.command=='GET':
            self.send(200,runtime.consumable_stats(),
                      extra_headers={'Cache-Control':'no-store'});return
        if path.startswith('/ut/game/fifa22/club/consumables') and self.command in ('GET','POST'):
            self.send(200,runtime.consumable_collection())
            return
        if path=='/ut/game/fifa22/club' and self.command in ('GET','POST'):
            filters={k:v[0] for k,v in parse_qs(urlsplit(self.path).query).items()}
            if body:
                try:filters.update(json.loads(body))
                except (ValueError,UnicodeDecodeError):
                    self.send(400,{'error':'INVALID_CLUB_FILTER'});return
                runtime.event('club_search_schema',keys=sorted(filters) if isinstance(filters,dict) else [],
                              value_type=type(filters).__name__,
                              type=str((filters or {}).get('type','') or ''))
            items=runtime.club.list_items('club')
            kind=str((filters or {}).get('type','player') or 'player').lower()
            seated=runtime.club_manager()
            if kind in ('kit','kits','badge','badges','ball','balls',
                        'stadium','stadiums','clubinfo','custom','equippables'):
                subtypes={'kit':{9},'kits':{9},'badge':{11},
                          'badges':{11},'ball':{30},'balls':{30},
                          # FIFA 22's active stadium slot advertises vanity
                          # subtype 10.  Keep 31 as a compatibility value for
                          # any older persisted card, but do not hide the
                          # native subtype-10 item from the Club catalogue.
                          'stadium':{10,31},'stadiums':{10,31},
                          'clubinfo':{9,10,11,31},
                          'custom':{9,10,11,30,31},
                          'equippables':{9,10,11,30,31}}[kind]
                owned=[item for item in items
                       if int(item.get('cardsubtypeid') or 0) in subtypes]
                owned=[runtime.club_cosmetic_wire(item) for item in owned]
                try:
                    start=max(0,int(filters.get('start',0)))
                    count=max(1,min(100,int(filters.get('count',50))))
                except (TypeError,ValueError,AttributeError):
                    self.send(400,{'error':'INVALID_CLUB_PAGE'});return
                self.send(200,{'itemData':owned[start:start+count],
                               'itemCount':len(owned)})
                return
            if kind in ('manager','managers','staff','coaches'):
                # FIFA 19 Club Items: type=manager/staff lists manager cards.
                # The previous handler always returned players, so the Staff
                # tab was empty even with a seated manager in the club.
                managers=[item for item in items
                          if item.get('itemType')=='manager']
                if not managers:
                    managers=[seated]
                else:
                    managers=[runtime.manager_item(
                        int(item.get('resourceId') or item.get('assetId') or
                            runtime.DEFAULT_MANAGER_ID),
                        int(item['id'])) for item in managers]
                    if not any(int(row['id'])==int(seated['id']) for row in managers):
                        managers.insert(0,seated)
                try:
                    start=max(0,int(filters.get('start',0)))
                    count=max(1,min(100,int(filters.get('count',50))))
                except (TypeError,ValueError,AttributeError):
                    self.send(400,{'error':'INVALID_CLUB_PAGE'});return
                self.send(200,{'itemData':managers[start:start+count],
                               'itemCount':len(managers)})
                return
            items=runtime.club.club_players(filters)
            items.sort(key=lambda item:(item['rating'],item['id']),reverse=True)
            try:
                start=max(0,int(filters.get('start',0)))
                count=max(1,min(100,int(filters.get('count',50))))
            except (TypeError,ValueError,AttributeError):
                self.send(400,{'error':'INVALID_CLUB_PAGE'});return
            payload=[runtime.player_item_wire(item)
                     for item in items[start:start+count]]
            # Match-start looks the seated manager up in the club collection.
            # A player-only list left Arteta drawing in the MGR slot (from
            # squad.manager) while the owned-item check still failed.
            include_manager=not any(k in filters for k in ('exclitem','exclid','exclude','excldef'))
            if include_manager and start==0 and seated['id'] not in {row.get('id') for row in payload}:
                payload.append(seated)
            total=len(items)+int(include_manager)
            self.send(200,{'itemData':payload,'itemCount':total})
            return
        if path=='/ut/game/fifa22/clientdata/userHubData':
            key='clientdata_userHubData'
            if self.command=='PUT':
                try:document=json.loads(body)
                except (ValueError,UnicodeDecodeError):
                    self.send(400,{'error':'INVALID_CLIENT_DATA'});return
                if not isinstance(document,dict):
                    self.send(400,{'error':'INVALID_CLIENT_DATA'});return
                runtime.club.set_meta(key,json.dumps(document,separators=(',',':')))
                self.send(200,document)
                return
            if self.command=='GET':
                raw=runtime.club.get_meta(key)
                self.send(200,json.loads(raw) if raw else {'entries':[{'key':0,'value':13},
                                                             {'key':1,'value':12}]})
                return
        if path=='/ut/game/fifa22/clientdata/onboarding' and self.command in ('GET','POST','PUT'):
            payload=runtime.completed_onboarding_client_data()
            payload.update(completed=True,onboardingComplete=1,firstTime=False,
                           returningUser=1)
            self.send(200,payload)
            return
        if path=='/ut/game/fifa22/sqbt/user/hub' and self.command=='GET':
            self.send(200,runtime.squad_battles_hub())
            return
        if path in ('/ut/game/fifa22/featuredsquad/user/stats',
                    '/ut/game/fifa22/sqbt/user/stats') and self.command=='GET':
            self.send(200,runtime.squad_battles_feature_stats())
            return
        if path.endswith('/featuredsquad/history') and self.command=='GET':
            self.send(200,{})
            return
        if path in ('/ut/game/fifa22/sqbt/v2/user/opponents/refresh',
                    '/ut/game/fifa22/sqbt/user/opponents/refresh') and self.command in (
                        'GET','POST','PUT'):
            self.send(200,runtime.squad_battles_refresh())
            return
        if path=='/ut/game/fifa22/sqbt/user/prize' and self.command in ('GET','POST'):
            self.send(200,runtime.squad_battles_prize())
            return
        if path=='/ut/game/fifa22/sqbt/leaderboard' and self.command=='GET':
            period=urlsplit(self.path).query.lower()
            self.send(200,runtime.squad_battles_leaderboard(previous='period=prev' in period))
            return
        opponent_squad=re.fullmatch(r'/ut/game/fifa22/sqbt/user/opponentsquad/(\d+)',path)
        if opponent_squad and self.command=='GET':
            result=runtime.squad_battles_opponent_squad(int(opponent_squad[1]))
            if result is None:
                # An RS4 404 disconnects this client. Empty squad is safer.
                self.send(200,{})
            else:
                runtime.event('sqbt_opponent_squad',opponentId=int(opponent_squad[1]),
                              players=len(result['players']))
                self.send(200,result)
            return
        # FIFA 22 Seasonal Campaign / Objectives service.  The native client
        # reads categoriesList -> groupsList -> objectives and the thirty
        # levels -> rewardOptionList hierarchy.
        objective_claim=re.fullmatch(
            r'/ut/game/fifa22/scmp/(?:campaign|learning)/group/(\d+)/objective/(\d+)/rewards',
            path)
        if objective_claim and self.command in ('GET','POST','PUT'):
            group_id,objective_id=map(int,objective_claim.groups())
            definition=SCMP.objective_definition(objective_id)
            if definition is None or int(definition[0]['groupId'])!=group_id:
                self.send(404,{'success':False,'reason':'OBJECTIVE_NOT_FOUND'});return
            status,response=runtime.objective_claim('objectives',objective_id)
            if status==200:response['objectiveId']=objective_id
            self.send(status,response);return
        group_claim=re.fullmatch(
            r'/ut/game/fifa22/scmp/(?:campaign|learning)/group/(\d+)/rewards',path)
        if group_claim and self.command in ('GET','POST','PUT'):
            status,response=runtime.objective_claim('groups',int(group_claim[1]))
            self.send(status,response);return
        level_claim=re.fullmatch(
            r'/ut/game/fifa22/scmp/campaign/level/(\d+)/reward/(\d+)',path)
        if level_claim and self.command in ('GET','POST','PUT'):
            level_id,option_id=map(int,level_claim.groups())
            status,response=runtime.objective_claim('levels',level_id,option_id)
            if status==200:
                response['successfulLevelRewards']=[level_id]
                response['awardedPrizes']=[{
                    'awardType':row['awardType'],'awardCount':row['count'],
                    'awardValue':row['value'],'halId':row['halId'],
                    'untradeable':row['isUntradeable'],'loan':0}
                    for row in response.get('awards',[])]
            self.send(status,response);return
        category_details=re.fullmatch(
            r'/ut/game/fifa22/scmp/(?:campaign/active|learning|active)/category/(\d+)/details',path)
        if category_details and self.command in ('GET','POST','PUT'):
            category_id=int(category_details[1])
            query=parse_qs(urlsplit(self.path).query)
            requested=set()
            def add_requested(value):
                if isinstance(value,dict):
                    for key,nested in value.items():
                        normalized=str(key).lower()
                        if normalized in ('groupid','groupids','groupidlist'):
                            add_requested(nested)
                        elif normalized in ('data','payload','filters','groups','grouplist'):
                            add_requested(nested)
                    return
                if isinstance(value,(list,tuple,set)):
                    for nested in value:add_requested(nested)
                    return
                for part in re.split(r'[,;\s]+',str(value or '')):
                    if part.isdigit():requested.add(int(part))
            add_requested(query.get('groupIdList',[]))
            add_requested(query.get('groupidlist',[]))
            body_format='none'
            body_keys=[]
            if body:
                try:
                    document=json.loads(body)
                    body_format='json'
                    if isinstance(document,dict):body_keys=sorted(document)
                    add_requested(document)
                except (ValueError,UnicodeDecodeError):
                    try:
                        form=parse_qs(body.decode('utf-8'))
                        body_format='form'
                        body_keys=sorted(form)
                        add_requested(form)
                    except UnicodeDecodeError:
                        body_format='binary'
            campaign=runtime.objectives_campaign()
            category=next((row for row in campaign.get('categoriesList',[])
                           if int(row.get('categoryId',0))==category_id),None)
            runtime.event('scmp_category_details',method=self.command,
                          route=path,categoryId=category_id,
                          requestedGroupIds=sorted(requested),
                          bodyFormat=body_format,bodyKeys=body_keys)
            headers={'Cache-Control':'no-store, no-cache, must-revalidate',
                     'Pragma':'no-cache',
                     'ETag':f'"scmp-{int(campaign.get("revision",0) or 0)}"'}
            if category is None:
                response={'categoryId':category_id,'groupsList':[],'groups':[],
                          'campaignId':SCMP.CAMPAIGN_ID,
                          'revision':int(campaign.get('revision',0) or 0)}
                self.send(404,response,extra_headers=headers);return
            groups=[row for row in category.get('groupsList',[])
                    if not requested or int(row.get('groupId',0)) in requested]
            # FutGetCategoryDetailsServerResponse is a category document, not
            # a campaign envelope. Repeating the campaign here produced a
            # 239-KB recursive-compatible superset that FIFA never finalized.
            response={'categoryId':category_id,
                      'name':category.get('name',''),
                      'type':int(category.get('type',0) or 0),
                      'priority':int(category.get('priority',0) or 0),
                      'groupsList':groups,'groups':groups,
                      'campaignId':SCMP.CAMPAIGN_ID,
                      'userXp':int(campaign.get('userXp',0) or 0),
                      'revision':int(campaign.get('revision',0) or 0)}
            self.send(200,response,extra_headers=headers);return
        if path in ('/ut/game/fifa22/scmp/campaign/active',
                    '/ut/game/fifa22/scmp/campaign',
                    '/ut/game/fifa22/scmp/world/campaign/active',
                    '/ut/game/fifa22/scmp/learning/campaign/active') \
                and self.command=='GET':
            campaign=runtime.objectives_campaign()
            revision=int(campaign.get('revision',0) or 0)
            self.send(200,campaign,extra_headers={
                'Cache-Control':'no-store, no-cache, must-revalidate',
                'Pragma':'no-cache','ETag':f'"scmp-{revision}"'})
            return
        if path in ('/ut/game/fifa22/scmp/campaign/rewards/all',
                    '/ut/game/fifa22/scmp/learning/campaign/rewards/all',
                    '/ut/game/fifa22/scmp/world/campaign/rewards/all') \
                and self.command=='GET':
            self.send(200,{'rewards':[],'rewardsList':[]});return
        if path=='/ut/game/fifa22/attributes/metadata' and self.command=='GET':
            # Must be a JSON array (FifaSharp List[PlayerAttributes]). Never return
            # an object — that hung SB. Avoid heavy catalogue work on empty boot
            # queries so title/FUT login stays responsive.
            raw=urlsplit(self.path)
            runtime.event('attributes_metadata_query', query=raw.query or '')
            ids=[]
            for chunk in parse_qs(raw.query).get('defIds',[]):
                for part in chunk.split(','):
                    part=part.strip()
                    if part.isdigit():
                        ids.append(int(part))
            if not ids:
                ids=list(getattr(runtime,'_sqbt_last_def_ids',[]) or [])
            if not ids:
                self.send(200,[])
                return
            catalogue=getattr(runtime,'_catalogue_cache',None)
            if catalogue is None:
                catalogue=load_catalogue()
                runtime._catalogue_cache=catalogue
            by_resource={int(row['resourceId']):row for row in catalogue['players']}
            payload=[]
            for def_id in ids:
                row=by_resource.get(original_id(def_id))
                face=list((row or {}).get('attributeArray') or [0]*6)
                payload.append({
                    'defId':def_id,
                    'rating':int((row or {}).get('rating') or 0),
                    'attributeArray':face[:7],
                })
            self.send(200,payload)
            return
        if path=='/ut/game/fifa22/stadium' and self.command in ('POST','PUT'):
            try:
                payload=json.loads(body or b'{}')
                owned=runtime.club.list_items('club')
                by_id={int(row['id']):row for row in owned
                       if str(row.get('itemType','')).lower() in
                       ('kit','custom','ball','stadium')}
                by_resource={}
                for row in by_id.values():
                    by_resource.setdefault(int(row.get('resourceId') or 0),[]).append(row)
                for rows in by_resource.values():
                    rows.sort(key=lambda row:(
                        0 if row.get('tradeable') is True else 1,
                        0 if str(row.get('itemState','')).startswith('active') else 1,
                        int(row['id'])))

                # FIFA 22 saves the complete badge/kit selection through the
                # stadium endpoint, rather than the single-item PUT used by
                # FIFA 20.  Accept its observed nested slot/item shapes and
                # the equivalent resource-id form used by other PC revisions.
                slot_states={1:'activeBall',2:'activeBadge',
                             3:'activeHomeKit',4:'activeAwayKit',
                             5:'activeStadium'}
                selected=[]

                def state_from_key(key):
                    key=str(key or '').lower().replace('_','')
                    if 'homekit' in key:return 'activeHomeKit'
                    if 'awaykit' in key:return 'activeAwayKit'
                    if 'badge' in key or 'crest' in key:return 'activeBadge'
                    if 'ball' in key:return 'activeBall'
                    if 'stadium' in key and key not in ('stadiumarea','stadiumid'):
                        return 'activeStadium'
                    return ''

                def resolve(value,state=''):
                    try:value=int(value)
                    except (TypeError,ValueError):return
                    if value in by_id:
                        item_id=value
                    elif value in by_resource and by_resource[value]:
                        item_id=int(by_resource[value][0]['id'])
                    else:return
                    selected.append((item_id,state))

                def collect(value,state='',key=''):
                    keyed_state=state_from_key(key) or state
                    if isinstance(value,dict):
                        row_state=str(value.get('itemState') or '')
                        if not row_state.startswith('active'):row_state=keyed_state
                        for slot_key in ('slotId','clientSlotId','slot'):
                            try:slot=int(value.get(slot_key,-1))
                            except (TypeError,ValueError):slot=-1
                            if slot in slot_states:
                                row_state=slot_states[slot];break
                        direct=False
                        for id_key in ('id','itemId','itemid','resourceId',
                                       'resourceid','definitionId','definitionid'):
                            if id_key in value:
                                resolve(value[id_key],row_state);direct=True
                        for child_key,child in value.items():
                            if child_key not in ('id','itemId','itemid','resourceId',
                                                'resourceid','definitionId','definitionid',
                                                'slotId','clientSlotId','slot','itemState'):
                                collect(child,row_state,child_key)
                    elif isinstance(value,list):
                        for child in value:collect(child,keyed_state,key)
                    elif isinstance(value,(int,str)):
                        resolve(value,keyed_state)

                collect(payload)
                # Preserve request order but activate an item only once. If
                # the body repeats one item through both itemData and a slot
                # wrapper, retain the most specific active-state marker.
                unique={}
                for item_id,state in selected:
                    if item_id not in unique or state:unique[item_id]=state
                if not unique:raise StateError('No owned club items in stadium update')
                updated=[]
                for item_id,state in unique.items():
                    card=by_id[item_id]
                    item_type=str(card.get('itemType','')).lower()
                    expected={'kit':{'activeHomeKit','activeAwayKit'},
                              'custom':{'activeBadge'},'ball':{'activeBall'},
                              'stadium':{'activeStadium'}}.get(item_type,set())
                    # Some client revisions number stadium slots from zero.
                    # If that makes the explicit slot contradict the actual
                    # item type, let activate_club_item infer the safe state.
                    if state not in expected:state=''
                    if item_type=='kit':
                        # FIFA 22's stadium payload has used both zero- and
                        # one-based kit slots. The item's native category is
                        # authoritative and prevents a home kit being rejected
                        # as an away kit (or vice versa), which was the 409 that
                        # made both selections revert after leaving FUT.
                        category=int(card.get('category') or 0)
                        if category==2:state='activeHomeKit'
                        elif category==3:state='activeAwayKit'
                    updated.append(runtime.club.activate_club_item(item_id,state))
            except (ValueError,TypeError,StateError,UnicodeDecodeError) as exc:
                self.send(409,{'error':'STADIUM_UPDATE_FAILED',
                               'reason':str(exc)});return
            runtime.event('stadium_saved',items=[{
                'id':int(row['id']),'resourceId':int(row['resourceId']),
                'itemState':row.get('itemState')} for row in updated])
            # The native client expects the same empty acknowledgement it
            # previously accepted; the following GET supplies the saved slots.
            self.send(200,{})
            return
        if path=='/ut/game/fifa22/stadium' and self.command=='GET':
            self.send(200,runtime.stadium_document())
            return
        if path=='/fut/items/keyAttributes.json' and self.command=='GET':
            # The working FIFA 22 bootstrap returns an empty JSON object here.
            # Invented members keep the shared Store/SBC/Objectives loader open.
            self.send(200,{},extra_headers={
                'Cache-Control':'no-store, no-cache, must-revalidate, max-age=0',
                'Pragma':'no-cache'})
            return
        if path=='/fut/seasonal/seasonalconfig.json' and self.command=='GET':
            # Match the known-good generic JSON fallback exactly. Objectives
            # campaign data is supplied by the native SCMP endpoints instead.
            self.send(200,{},extra_headers={
                'Cache-Control':'no-store, no-cache, must-revalidate, max-age=0',
                'Pragma':'no-cache'})
            return
        if path in ('/fut/items/images/backgrounds/itemBGs/futitemraritytunables.json',
                    '/fut/packs/packopening/packopeningsetting.json',
                    '/fut/packs/packopening/packopeningconfig.json') and self.command=='GET':
            source=FIFA22_CONTENT/path.removeprefix('/fut/')
            if source.is_file():
                raw=source.read_bytes()
                if path.endswith('/futitemraritytunables.json'):
                    raw=rarity_manifest_with_specials(raw)
                runtime.event('fifa22_content',path=path,bytes=len(raw))
                self.send(200,raw,'application/json')
                return
        if (path.startswith('/fut/items/images/backgrounds/itemBGs/')
                and path.endswith('.dds') and self.command=='GET'):
            # The client inserts an asset GUID that is not part of the local
            # extracted directory tree. Match the requested filename only
            # within the FIFA 22 card-background asset directories.
            name=Path(path).name
            matches=list((FIFA22_CONTENT/'items/images/backgrounds/itemBGs').rglob(name))
            if len(matches)==1:
                raw=matches[0].read_bytes()
                runtime.event('fifa22_content',path=path,bytes=len(raw))
                self.send(200,raw,'application/octet-stream')
                return
            runtime.event('fut_optional_missing',path=path)
            self.send(204,b'','application/octet-stream')
            return
        actionshot=re.fullmatch(
            r'/(?:fut/items/|items/)?images/actionShot/d2(?:[0-9a-f]{6})?/p([0-9]+)\.dds',path)
        if actionshot and self.command=='GET':
            resource_id=int(actionshot[1])
            source=ACTIONSHOT_ROOT/f'p{resource_id}.dds'
            if str(resource_id) in ACTIONSHOT_IDS and source.is_file():
                raw=source.read_bytes()
                runtime.event('fifa22_actionshot',resourceId=resource_id,bytes=len(raw))
                self.send(200,raw,'application/octet-stream',
                          # The d2<manifest-hash> namespace changes whenever
                          # the artwork set changes, so this exact DDS is safe
                          # to cache forever. Re-decoding it on every menu and
                          # match transition was exhausting FIFA's texture
                          # cache and corrupting later Store/card renders.
                          {'Cache-Control':'public, max-age=31536000, immutable'})
            else:
                runtime.event('fut_optional_missing',path=path)
                # A zero-byte 204 is still a successful texture fetch to the
                # Frostbite resource loader. A real 404 preserves the native
                # face/atlas fallback instead of installing a blank texture.
                self.send(404,b'','application/octet-stream',
                          {'Cache-Control':'no-store'})
            return
        head=re.fullmatch(r'/fut/playerheads/g4/single/p([0-9]+)\.dds',path)
        if head and self.command=='GET':
            source=FIFA22_HEADS.parent/'playerheads'/f'p{original_id(int(head[1]))}.dds'
            if source.is_file():
                raw=source.read_bytes()
                runtime.event('fifa22_dynamic_head',resourceId=int(head[1]),bytes=len(raw))
                self.send(200,raw,'application/octet-stream',
                          {'Cache-Control':'no-store, no-cache, must-revalidate, max-age=0',
                           'Pragma':'no-cache'})
            else:
                runtime.event('fut_optional_missing',path=path)
                self.send(404,b'','application/octet-stream',
                          {'Cache-Control':'no-store'})
            return
        if path=='/fut/playerheads/g4/fut2dheads.big' and self.command=='GET':
            # The supplied known-good server has no aggregate portrait archive;
            # it returns this valid empty BIGF container. The custom 47 MB BIG4
            # archive was the only bootstrap resource shared by every screen
            # that stalled, and it was also the asset corrupted after a match.
            raw=(b'BIGF'+struct.pack('<I',16)+struct.pack('>I',0)
                 +struct.pack('>I',16))
            runtime.event('fifa22_player_heads_empty',bytes=len(raw))
            self.send(200,raw,'application/octet-stream',{
                'Cache-Control':'no-store, no-cache, must-revalidate, max-age=0',
                'Pragma':'no-cache'})
            return
        if path in COSMETIC_FILES and self.command=='GET':
            source=REFERENCE_COSMETICS/COSMETIC_FILES[path]
            raw=source.read_bytes()
            runtime.event('reference_cosmetic',path=path,source_game='fifa20',bytes=len(raw))
            self.send(200,raw,'application/json' if path.endswith('.json') else 'application/octet-stream')
            return
        runtime.event('unimplemented_http',method=self.command,path=path)
        # Project FUT22's bisection notes prove the successful runs answered
        # 200 for every FUT document while 404/5xx makes the client error out
        # of FUT within seconds. Serve the parser-safe empty object and keep
        # the route visible in events.jsonl for follow-up work.
        if path.startswith(('/ut/','/fut/')):
            self.send(200,{},extra_headers={'Cache-Control':'no-store'});return
        self.send(501,{'error':'UNIMPLEMENTED_FIFA22_CONTRACT','path':path})

    do_GET=do_POST=do_PUT=do_DELETE=do_OPTIONS=go

class TLSHttpServer(HttpServer):
    def get_request(self):
        connection,address=super().get_request()
        connection.settimeout(10)
        try:
            tls=self.tls_context.wrap_socket(connection,server_side=True)
            self.runtime.event('tls_handshake',version=tls.version(),cipher=tls.cipher()[0])
            return tls,address
        except Exception as exc:
            connection.close()
            self.runtime.event('tls_failure',error=str(exc))
            raise OSError(str(exc)) from exc

def make_servers(runtime,http_port,blaze_port,redirector_port,cert=None,key=None,qos_port=0):
    servers=[]
    try:
        http=HttpServer(('127.0.0.1',http_port),HttpHandler)
        servers.append(http)
        http.runtime=runtime;http.is_redirector=False;http.is_qos=False
        runtime.http_port=http.server_address[1]
        blaze=BlazeServer(('127.0.0.1',blaze_port),BlazeHandler)
        servers.append(blaze);blaze.runtime=runtime
        runtime.blaze_port=blaze.server_address[1]
        if bool(cert)!=bool(key):raise ValueError('Provide both certificate and key')
        cls=TLSHttpServer if cert else HttpServer
        redirector=cls(('127.0.0.1',redirector_port),HttpHandler)
        servers.append(redirector)
        redirector.runtime=runtime;redirector.is_redirector=True;redirector.is_qos=False
        runtime.redirector_tls=bool(cert)
        if cert:
            ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.minimum_version=ssl.TLSVersion.TLSv1_2
            ctx.maximum_version=ssl.TLSVersion.TLSv1_2
            ctx.load_cert_chain(str(cert),str(key))
            redirector.tls_context=ctx
        qos=HttpServer(('127.0.0.1',qos_port),HttpHandler)
        servers.append(qos)
        qos.runtime=runtime;qos.is_redirector=False;qos.is_qos=True
        runtime.qos_port=qos.server_address[1]
        return servers
    except Exception:
        for server in servers:server.server_close()
        raise

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime',type=Path,default=Path(__file__).resolve().parents[1]/'runtime')
    parser.add_argument('--http-port',type=int,default=18222)
    parser.add_argument('--blaze-port',type=int,default=18221)
    parser.add_argument('--redirector-port',type=int,default=18220)
    parser.add_argument('--qos-port',type=int,default=17502)
    parser.add_argument('--preauth-qos',action='store_true')
    parser.add_argument('--sqbt-native-opponent',action='store_true')
    parser.add_argument('--cert',type=Path)
    parser.add_argument('--key',type=Path)
    args=parser.parse_args()
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(message)s')
    runtime=Runtime(args.runtime,args.http_port,args.blaze_port,args.qos_port,args.preauth_qos,
                    args.sqbt_native_opponent)
    servers=make_servers(runtime,args.http_port,args.blaze_port,args.redirector_port,args.cert,args.key,args.qos_port)
    workers=[]
    try:
        for server in servers:
            thread=threading.Thread(target=server.serve_forever,daemon=True)
            thread.start();workers.append(thread)
        runtime.event('services_started',http=runtime.http_port,blaze=runtime.blaze_port,redirector=servers[2].server_address[1],qos=runtime.qos_port,preauth_qos=runtime.preauth_qos,tls=bool(args.cert))
        print('FIFA 22 local FUT service running (FUT managercards id, hub omits squad).',flush=True)
        while True:
            time.sleep(5)
            try:runtime.club.market_tick()
            except Exception:log.exception('Local market tick failed')
    except KeyboardInterrupt:
        pass
    finally:
        for server in servers:server.shutdown();server.server_close()
        for worker in workers:worker.join(timeout=3)

if __name__=='__main__':main()

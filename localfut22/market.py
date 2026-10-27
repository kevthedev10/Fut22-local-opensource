"""Persistent local auctions. Bot valuations are simulated, not live prices."""
import json
import time
from .icon_identity import original_id, presented_asset


class StateError(ValueError):
    pass


def query_values(query):
    return {str(k).lower(): (v[0] if isinstance(v,list) and v else v)
            for k,v in (query or {}).items()}


def number(query,*keys):
    for key in keys:
        value=query.get(key)
        if value not in (None,'','any','-1',-1):return int(value)
    return None


MARKET_TARGET_ACTIVE=400_000


def round_price(value):
    """Clamp a value to FIFA's transfer-price ladder."""
    value=max(150,min(15_000_000,int(value)))
    step=increment(value)
    return max(150,int(round(value/step))*step)


def matches(card,query,price=None,bid=None):
    q=query_values(query)
    kind=str(q.get('type','player')).lower()
    kind={'1':'player','2':'manager'}.get(kind,kind)
    if kind not in ('any','',str(card.get('itemType','player')).lower()):return False
    rid=int(card['resourceId']);aid=int(card.get('assetId',rid))
    exact=number(q,'definitionid','defid')
    masked=number(q,'maskeddefid')
    if exact and original_id(exact)!=rid:return False
    if masked:
        if masked>=1<<24:
            if original_id(masked)!=rid:return False
        elif masked not in (aid,presented_asset(rid,aid)):return False
    for keys,value in ((('nat','nation'),card.get('nationId',card.get('nation',0))),
                       (('leag','league'),card.get('leagueId',0)),
                       (('team','club'),card.get('teamid',card.get('teamId',0))),
                       (('playstyle','chemistrystyle'),card.get('playStyle') or (273 if card.get('preferredPosition')=='GK' else 250)),
                       (('cardsubtype',),card.get('cardsubtypeid',0))):
        wanted=number(q,*keys)
        if wanted is not None and wanted>0 and wanted!=int(value or 0):return False
    rating=int(card.get('rating',0))
    level=str(q.get('lev',q.get('level','any'))).lower()
    quality='bronze' if rating<65 else 'silver' if rating<75 else 'gold'
    special=int(card.get('rareflag',0))>1 or card.get('special',False)
    if level in ('bronze','silver','gold') and (quality!=level or special):return False
    if level in ('special','sp') and not special:return False
    rare=str(q.get('rare','any')).lower()
    if rare in ('sp','special','true') and not special:return False
    if rare in ('common','nonrare'):rare='0'
    if rare=='rare':rare='1'
    if rare in ('0','1') and int(card.get('rareflag',0))!=int(rare):return False
    rarities=str(q.get('rarityids',q.get('rarityid','')))
    if rarities and rarities not in ('any','-1'):
        if int(card.get('rareflag',0)) not in {int(v) for v in rarities.split(',') if v}:return False
    pos=str(q.get('pos',q.get('position','any'))).upper()
    actual=str(card.get('preferredPosition','')).upper()
    position_groups={'DEFENSE':'DEF','DEFENDER':'DEF','MIDFIELD':'MID',
                     'MIDFIELDER':'MID','ATTACKER':'ATT','FORWARD':'ATT'}
    if pos not in ('ANY','-1',''):
        grouped=position_groups.get(pos)
        groups={'DEF':{'CB','LB','RB','LWB','RWB'},
                'MID':{'CDM','CM','CAM','LM','RM'},
                'ATT':{'LW','RW','LF','RF','CF','ST'}}
        if (grouped and actual not in groups[grouped]) or (not grouped and actual!=pos):
            return False
    zone=str(q.get('zone','any')).upper()
    zones={'GK':{'GK'},'DEF':{'CB','LB','RB','LWB','RWB'},
           'MID':{'CDM','CM','CAM','LM','RM'},'ATT':{'LW','RW','LF','RF','CF','ST'}}
    zone={'DEFEND':'DEF','DEFENSE':'DEF','DEFENDER':'DEF',
          'MIDFIELD':'MID','MIDFIELDER':'MID','ATTACK':'ATT',
          'ATTACKER':'ATT','FORWARD':'ATT'}.get(zone,zone)
    if zone in zones and actual not in zones[zone]:return False
    min_rating=number(q,'minrating','minr')
    max_rating=number(q,'maxrating','maxr')
    if min_rating is not None and rating<min_rating:return False
    if max_rating is not None and rating>max_rating:return False
    if price is not None:
        # FUT uses minb/maxb for the current/start bid and micr/macr for
        # Buy Now.  Keeping these separate is essential for Compare Price.
        for key,value,lower in (('minb',bid,True),('maxb',bid,False),
                                ('micr',price,True),('macr',price,False)):
            bound=number(q,key)
            if bound is not None and (value<bound if lower else value>bound):return False
    return True


def valuation(card):
    """Stable card-aware local value model shared with FIFA 20."""
    rating=int(card.get('rating',65))
    anchors={75:700,76:750,77:800,78:900,79:1_000,80:1_200,
             81:1_500,82:2_000,83:3_000,84:5_000,85:9_000,
             86:15_000,87:28_000,88:48_000,89:80_000,90:125_000,
             91:200_000,92:300_000,93:450_000,94:650_000,
             95:900_000,96:1_200_000,97:1_700_000,
             98:2_500_000,99:4_000_000}
    if rating<65:base=max(150,200+max(0,rating-45)*20)
    elif rating<75:base=350+(rating-65)*65
    else:base=anchors.get(rating,4_000_000)
    rareflag=int(card.get('rareflag',0) or 0)
    if card.get('special') or rareflag>1:
        base=int(base*{3:1.22,4:1.28,5:1.75,6:1.35,8:1.32,
                       9:1.38,10:1.35,11:1.55,12:1.65}.get(rareflag,1.30))
    face=card.get('attributeArray',card.get('face',[]))
    if isinstance(face,list) and face:
        useful=sorted((int(value or 0) for value in face),reverse=True)[:3]
        quality=sum(useful)/max(1,len(useful))
        base=int(base*max(.88,min(1.18,.88+quality/300.0)))
    rid=int(card.get('resourceId') or card.get('assetId') or 0)
    return round_price(base*(.92+(rid*37%17)/100.0))


def quicksell(card):
    """Retail-shaped discard value; always safely below market value."""
    rating=int(card.get('rating',50) or 50)
    rare=1 if int(card.get('rareflag',0) or 0)>0 else 0
    special=bool(card.get('special')) or int(card.get('rareflag',0) or 0)>1
    if special:
        value=min(17_000,10_000+max(0,rating-75)*350)
    elif rating<=64:value=(100,200)[rare]
    elif rating<=74:value=(200,400)[rare]
    elif rating<=81:value=(400,700)[rare]
    elif rating<=85:value=(700,1_200)[rare]
    else:value=(1_200,1_200+(rating-86)*300)[rare]
    return max(50,min(value,max(100,valuation(card)*4//5)))


def price_bounds(card):
    recommended=valuation(card)
    return (max(150,round_price(recommended*.45)),
            min(15_000_000,max(5_000,round_price(recommended*2.5))))


def increment(bid):
    return 50 if bid<1000 else 100 if bid<10000 else 250 if bid<50000 else 500 if bid<100000 else 1000


def starting_price(price):
    amount=max(150,int(price)//3)
    return amount//increment(amount)*increment(amount)


def listing_card(card):
    """Stable per-edition chemistry styles, preserved on acquisition."""
    card=dict(card)
    seed=(int(card['resourceId'])*2654435761)&0xffffffff
    styles=(269,270,271,272) if card.get('preferredPosition')=='GK' else tuple(range(251,269))
    card['playStyle']=styles[(seed>>8)%len(styles)] if seed%100<45 else 0
    return card


class Market:
    @staticmethod
    def migrate_market(db):
        additions={'market_trades':{'held':'INTEGER NOT NULL DEFAULT 0',
            'user_bid':'INTEGER NOT NULL DEFAULT 0','highest':"TEXT NOT NULL DEFAULT ''",
            'watched':'INTEGER NOT NULL DEFAULT 0','bot_next':'INTEGER NOT NULL DEFAULT 0'},
            'user_listings':{'current_bid':'INTEGER NOT NULL DEFAULT 0',
                            'bot_next':'INTEGER NOT NULL DEFAULT 0','proceeds':'INTEGER NOT NULL DEFAULT 0'}}
        for table,columns in additions.items():
            existing={r[1] for r in db.execute(f'PRAGMA table_info({table})')}
            for column,definition in columns.items():
                if column not in existing:db.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')
        if not db.execute("SELECT 1 FROM meta WHERE key='marketSchema2'").fetchone():
            db.execute("UPDATE market_trades SET user_bid=bid,held=CASE WHEN state='active' THEN bid ELSE 0 END,highest='user',watched=1 WHERE bid>0")
            db.execute("INSERT INTO meta VALUES('marketSchema2','1')")

    @staticmethod
    def _coins(db,delta):
        current=int(db.execute("SELECT value FROM meta WHERE key='credits'").fetchone()[0])
        if current+delta<0:raise StateError('Insufficient coins')
        db.execute("UPDATE meta SET value=? WHERE key='credits'",(str(current+delta),))
        return current+delta

    @staticmethod
    def _reserve(db,trade,card):
        if trade['item_id']:return int(trade['item_id'])
        item=db.execute('INSERT INTO items(resource_id,pile,doc) VALUES(?,?,?)',
            (trade['resource_id'],'market',json.dumps(dict(card,untradeable=False,tradeable=True)))).lastrowid
        db.execute('UPDATE market_trades SET item_id=? WHERE trade_id=?',(item,trade['trade_id']))
        return item

    def _market_settle_due(self,db,now=None):
        now=int(time.time()) if now is None else int(now)
        # Expiry is settled before new bot decisions: a late poll cannot invent
        # a bid after the auction has already ended.
        for t in db.execute("SELECT * FROM market_trades WHERE state='active' AND ends_at<=? AND bid>0",(now,)).fetchall():
            if t['highest']=='user':
                doc=db.execute('SELECT doc FROM definitions WHERE resource_id=?',(t['resource_id'],)).fetchone()
                if not doc:continue
                item=self._reserve(db,t,json.loads(doc[0]))
                db.execute("UPDATE items SET pile='unassigned' WHERE id=?",(item,))
                db.execute("UPDATE market_trades SET state='sold',held=0 WHERE trade_id=?",(t['trade_id'],))
            else:
                self._coins(db,int(t['held']))
                db.execute("UPDATE market_trades SET state='expired',held=0 WHERE trade_id=?",(t['trade_id'],))
        for t in db.execute("SELECT * FROM market_trades WHERE state='active' AND bid>0 AND ends_at>? AND bot_next>0 AND bot_next<=?",(now,now)).fetchall():
            ceiling=int(t['price']*(0.60+(t['trade_id']%29)/100))
            offer=int(t['bid'])+increment(int(t['bid']))
            if t['highest']=='user' and offer<=ceiling:
                self._coins(db,int(t['held']))
                db.execute("UPDATE market_trades SET bid=?,highest='bot',held=0,bot_next=0 WHERE trade_id=?",(offer,t['trade_id']))
            else:db.execute('UPDATE market_trades SET bot_next=0 WHERE trade_id=?',(t['trade_id'],))
        for t in db.execute("SELECT u.*,i.doc FROM user_listings u JOIN items i ON i.id=u.item_id WHERE u.state='active'").fetchall():
            if not t['bot_next'] and t['ends_at']>now:
                db.execute('UPDATE user_listings SET bot_next=? WHERE trade_id=?',(now+45+t['trade_id']%120,t['trade_id']))
            value=valuation(json.loads(t['doc']))
            ceiling=int(value*(0.83+(t['trade_id']%31)/100))
            bid=int(t['current_bid']);sold=False
            if t['ends_at']<=now:
                sold=bid>0
                if not sold:db.execute("UPDATE user_listings SET state='expired' WHERE trade_id=?",(t['trade_id'],))
            elif t['bot_next'] and t['bot_next']<=now:
                # Modest demand and a bounded valuation: overpriced listings
                # can expire. No instant guaranteed sales at arbitrary prices.
                if t['buy_now'] and t['buy_now']<=ceiling and t['trade_id']%3!=0:
                    bid=int(t['buy_now']);sold=True
                else:
                    offer=bid+increment(bid) if bid else int(t['starting_bid'])
                    if offer<=ceiling:bid=offer
                db.execute('UPDATE user_listings SET current_bid=?,bot_next=? WHERE trade_id=?',
                    (bid,now+60+t['trade_id']%180,t['trade_id']))
            if sold:
                proceeds=bid*95//100
                self._coins(db,proceeds)
                db.execute("UPDATE user_listings SET state='closed',current_bid=?,proceeds=? WHERE trade_id=?",(bid,proceeds,t['trade_id']))
                db.execute("UPDATE items SET pile='sold' WHERE id=?",(t['item_id'],))

    def market_tick(self,now=None):
        with self.transaction() as db:self._market_settle_due(db,now)

    @staticmethod
    def _market_record(t,card):
        state=t['state'];bid=int(t['bid'])
        bid_state=('buyNow' if state=='sold' and bid>=t['price'] else 'highest') if t['highest']=='user' else ('outbid' if t['user_bid'] else 'none')
        return {'tradeId':int(t['trade_id']),'resourceId':int(t['resource_id']),
            'price':int(t['price']),'startingBid':starting_price(t['price']),'bid':bid,
            'state':state,'bidState':bid_state,'watched':bool(t['watched']),
            'itemId':int(t['item_id']),'card':card,
            'expires':max(0,int(t['ends_at'])-int(time.time())) if state=='active' else 0}

    def market_listings(self,asset_id=None,start=0,count=11,max_price=None,filters=None):
        if start<0 or not 1<=count<=50:raise StateError('Invalid market page')
        q=query_values(filters)
        if asset_id is not None:
            if asset_id<1:raise StateError('Invalid market player ID')
            q['maskeddefid']=asset_id
        if max_price is not None:q['maxb']=max_price
        with self.transaction() as db:
            self._market_settle_due(db)
            now=int(time.time());visible=[]
            # The user's auctions belong only in Transfer List, never their own
            # public search results.
            # Stable identity order keeps a named player's base edition before
            # expensive promos; filters still expose every exact version.
            rows=db.execute('SELECT resource_id,doc FROM definitions ORDER BY resource_id')
            for row in rows:
                card=listing_card(json.loads(row['doc']))
                if not matches(card,q):continue
                rid=int(row['resource_id']);price=valuation(card)
                # Current-bid limits are applied to the persisted auction below,
                # not its original starting price.
                if not matches(card,{k:v for k,v in q.items()
                                     if k not in ('minb','maxb','micr','macr')},
                               price,0):continue
                trades=db.execute('SELECT * FROM market_trades WHERE resource_id=? ORDER BY trade_id',(rid,)).fetchall()
                if any(t['state']=='sold' and t['highest']=='user' for t in trades):
                    # Older builds created a replacement immediately after a
                    # purchased card entered the club. Retire only unbid bot
                    # replacements; preserve auctions with user bids.
                    for t in trades:
                        if (t['state']=='active' and not t['bid']
                                and not t['user_bid'] and not t['held']):
                            db.execute("UPDATE market_trades SET state='expired' WHERE trade_id=?",
                                       (t['trade_id'],))
                    trades=db.execute('SELECT * FROM market_trades WHERE resource_id=? ORDER BY trade_id',(rid,)).fetchall()
                # Compare Price formerly minted variants 1..7 for the selected
                # edition. Hide those persisted synthetic rows from every public
                # search while retaining any bids/watchlist history on them.
                legacy_base=3_000_000_000+rid*32
                live=[t for t in trades if t['state']=='active' and not
                      legacy_base<int(t['trade_id'])<legacy_base+8]
                # Seed an edition once. A sold listing must not be recreated
                # when its item moves from Unassigned into the club or squad.
                active_target=0 if trades else 1
                for variant in range(len(live),active_target):
                    trade_id=3_000_000_000+rid*32+variant
                    if db.execute('SELECT 1 FROM market_trades WHERE trade_id=?',(trade_id,)).fetchone():
                        trade_id=max(4_000_000_000,int(db.execute('SELECT COALESCE(MAX(trade_id),0)+1 FROM market_trades').fetchone()[0]))
                    ask=round_price(price*(.92+((rid+variant*13)%17)/100.0))
                    db.execute('INSERT INTO market_trades(trade_id,resource_id,price,ends_at) VALUES(?,?,?,?)',
                        (trade_id,rid,ask,now+3600+(rid+variant*977)%18000))
                    live.append(db.execute('SELECT * FROM market_trades WHERE trade_id=?',(trade_id,)).fetchone())
                for t in live:
                    if t['state']=='active' and not t['bid'] and t['ends_at']<=now:
                        db.execute('UPDATE market_trades SET ends_at=? WHERE trade_id=?',(now+21600,t['trade_id']))
                    self._reserve(db,t,card)
                    t=db.execute('SELECT * FROM market_trades WHERE trade_id=?',(t['trade_id'],)).fetchone()
                    if matches(card,q,t['price'],t['bid'] or starting_price(t['price'])):
                        visible.append(self._market_record(t,card))
                if len(visible)>=start+count:break
            return visible[start:start+count]

    def market_trades(self,trade_ids=None,bids_only=False):
        with self.transaction() as db:
            self._market_settle_due(db)
            selected=[]
            for t in db.execute('SELECT * FROM market_trades ORDER BY trade_id').fetchall():
                if trade_ids is not None and t['trade_id'] not in trade_ids:continue
                if bids_only and not (t['watched'] or t['user_bid']):continue
                if trade_ids is None and t['state']=='sold' and not db.execute("SELECT 1 FROM items WHERE id=? AND pile='unassigned'",(t['item_id'],)).fetchone():continue
                definition=db.execute('SELECT doc FROM definitions WHERE resource_id=?',(t['resource_id'],)).fetchone()
                if definition:
                    card=listing_card(json.loads(definition[0]));item_id=self._reserve(db,t,card)
                    stored=db.execute('SELECT doc FROM items WHERE id=?',(item_id,)).fetchone()
                    if stored:card=json.loads(stored[0])
                    t=db.execute('SELECT * FROM market_trades WHERE trade_id=?',(t['trade_id'],)).fetchone()
                    selected.append(self._market_record(t,card))
            return selected

    def market_offer(self,trade_id,amount):
        if type(trade_id) is not int or type(amount) is not int or amount<1:raise StateError('Invalid market offer')
        with self.transaction() as db:
            self._market_settle_due(db)
            t=db.execute('SELECT * FROM market_trades WHERE trade_id=?',(trade_id,)).fetchone()
            if not t:raise StateError('Unknown market listing')
            card=listing_card(json.loads(db.execute('SELECT doc FROM definitions WHERE resource_id=?',(t['resource_id'],)).fetchone()[0]))
            if t['item_id']:
                stored=db.execute('SELECT doc FROM items WHERE id=?',(t['item_id'],)).fetchone()
                if stored:card=json.loads(stored[0])
            if t['state']=='sold' and t['highest']=='user':return self._market_record(t,card),self._coins(db,0)
            if t['state']!='active':raise StateError('Auction has ended')
            buy=amount>=t['price']
            minimum=t['bid']+increment(t['bid']) if t['bid'] else starting_price(t['price'])
            if not buy and t['highest']=='user' and amount==t['bid']:return self._market_record(t,card),self._coins(db,0)
            if not buy and amount<minimum:raise StateError('Bid below minimum increment')
            amount=int(t['price']) if buy else amount
            credits=self._coins(db,int(t['held'])-amount)
            item=self._reserve(db,t,card);now=int(time.time())
            if buy:db.execute("UPDATE items SET pile='unassigned' WHERE id=?",(item,))
            db.execute('UPDATE market_trades SET bid=?,user_bid=?,held=?,highest=?,state=?,watched=1,ends_at=?,bot_next=? WHERE trade_id=?',
                (amount,amount,0 if buy else amount,'user','sold' if buy else 'active',
                 now if buy else max(now+30,t['ends_at']),0 if buy else now+25+trade_id%50,trade_id))
            updated=db.execute('SELECT * FROM market_trades WHERE trade_id=?',(trade_id,)).fetchone()
            return self._market_record(updated,card),credits

    def list_for_sale(self,item_id,starting_bid,buy_now,duration):
        if any(type(v) is not int for v in (item_id,starting_bid,buy_now,duration)):raise StateError('Invalid auction values')
        if item_id<1 or starting_bid<150 or (buy_now and buy_now<starting_bid) or max(buy_now,starting_bid)>15_000_000 or duration not in (3600,10800,21600,43200,86400,259200):raise StateError('Invalid auction price or duration')
        with self.transaction() as db:
            self._market_settle_due(db)
            item=db.execute('SELECT * FROM items WHERE id=?',(item_id,)).fetchone()
            if not item or item['pile'] not in ('club','trade'):raise StateError('Card is not available for listing')
            card=json.loads(item['doc'])
            if card.get('untradeable',True) or not card.get('tradeable',False):raise StateError('Card is untradeable')
            prior=db.execute('SELECT * FROM user_listings WHERE item_id=?',(item_id,)).fetchone()
            if prior and prior['state']=='active':return int(prior['trade_id'])
            trade_id=max(1_000_000_000,int(db.execute('SELECT COALESCE(MAX(trade_id),999999999)+1 FROM user_listings').fetchone()[0]))
            db.execute('DELETE FROM user_listings WHERE item_id=?',(item_id,))
            now=int(time.time())
            db.execute('INSERT INTO user_listings(trade_id,item_id,starting_bid,buy_now,ends_at,bot_next) VALUES(?,?,?,?,?,?)',
                (trade_id,item_id,starting_bid,buy_now,now+duration,now+45+trade_id%120))
            db.execute("UPDATE items SET pile='trade' WHERE id=?",(item_id,))
            for squad in db.execute('SELECT id,doc FROM squads').fetchall():
                slots=json.loads(squad['doc'])
                if item_id in slots:db.execute('UPDATE squads SET doc=? WHERE id=?',(json.dumps([0 if x==item_id else x for x in slots]),squad['id']))
            return trade_id

    def transfer_list(self,trade_ids=None):
        with self.transaction() as db:
            self._market_settle_due(db);now=int(time.time());result=[]
            for r in db.execute("SELECT i.id,i.resource_id,i.doc,u.* FROM items i LEFT JOIN user_listings u ON u.item_id=i.id WHERE i.pile IN ('trade','sold') ORDER BY i.id"):
                tid=int(r['trade_id'] or 0)
                if trade_ids is not None and tid not in trade_ids:continue
                state=r['state'] or 'inactive'
                result.append({'tradeId':tid,'itemId':int(r['id']),
                    'card':dict(json.loads(r['doc']),id=int(r['id']),resourceId=int(r['resource_id'])),
                    'startingBid':int(r['starting_bid'] or 0),'buyNowPrice':int(r['buy_now'] or 0),
                    'bid':int(r['current_bid'] or 0),'coinsProcessed':int(r['proceeds'] or 0),
                    'state':state,'expires':max(0,int(r['ends_at'] or 0)-now) if state=='active' else 0})
            return result

    def watch_trade(self,ids,watch=True):
        with self.transaction() as db:
            for tid in ids:
                if not watch:
                    row=db.execute('SELECT * FROM market_trades WHERE trade_id=?',(tid,)).fetchone()
                    if row and (row['held'] or row['state']=='sold' and db.execute("SELECT 1 FROM items WHERE id=? AND pile='unassigned'",(row['item_id'],)).fetchone()):continue
                    db.execute('UPDATE market_trades SET user_bid=0 WHERE trade_id=?',(tid,))
                db.execute('UPDATE market_trades SET watched=? WHERE trade_id=?',(int(watch),tid))

    def clear_sold(self,trade_id=None):
        with self.transaction() as db:
            self._market_settle_due(db)
            rows=db.execute("SELECT * FROM user_listings WHERE state='closed'").fetchall()
            for r in rows:
                if trade_id is None or r['trade_id']==trade_id:
                    db.execute("UPDATE items SET pile='archived' WHERE id=? AND pile='sold'",(r['item_id'],))
                    db.execute("UPDATE user_listings SET state='cleared' WHERE trade_id=?",(r['trade_id'],))

    def relist_expired(self):
        with self.transaction() as db:
            self._market_settle_due(db)
            now=int(time.time())
            db.execute("UPDATE user_listings SET state='active',ends_at=?,bot_next=?,current_bid=0 WHERE state='expired' AND item_id IN (SELECT id FROM items WHERE pile='trade')",(now+3600,now+90))

    def transfer_counts(self):
        pile=self.transfer_list();targets=self.market_trades(bids_only=True)
        with self.transaction() as db:
            unavailable=int(db.execute(
                "SELECT COUNT(*) FROM market_trades WHERE state='sold'").fetchone()[0])
        selling=sum(t['state']=='active' for t in pile)
        sold=sum(t['state']=='closed' for t in pile)
        won=sum(t['state']=='sold' for t in targets)
        winning=sum(t['state']=='active' and t['bidState']=='highest' for t in targets)
        outbid=sum(t['state']=='active' and t['bidState']=='outbid' for t in targets)
        live=max(0,MARKET_TARGET_ACTIVE-unavailable)+selling
        return {'auctionCount':live,'liveAuctions':live,'liveTransfers':live,
            'tradePile':{'count':len(pile),'selling':selling,'sold':sold,
                         'notification':sold+sum(t['state']=='expired' for t in pile)},
            'watchlist':{'count':len(targets),'winning':winning,'outbid':outbid,'won':won,'notification':won}}

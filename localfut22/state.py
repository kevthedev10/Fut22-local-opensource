"""Transactional local club state. Catalogues must explicitly identify FIFA 22.

Reference-year player data is never silently promoted to a FIFA 22 catalogue.
Wire serialization belongs to the title adapter, not this storage layer.
"""
from contextlib import contextmanager
import json
from pathlib import Path
import random
import sqlite3
import time
from .icon_identity import presented_asset

from .market import Market, StateError, matches, query_values, quicksell

PLAYER_PICK_RESOURCE_ID=5_004_241
PLAYER_PICK_CARD_ASSET_ID=44

class Club(Market):
    def __init__(self,path):
        self.path=Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.transaction() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS definitions(resource_id INTEGER PRIMARY KEY,doc TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS items(id INTEGER PRIMARY KEY AUTOINCREMENT,resource_id INTEGER NOT NULL,pile TEXT NOT NULL,doc TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS squads(id INTEGER PRIMARY KEY,doc TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS packs(id INTEGER PRIMARY KEY,doc TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY,request TEXT NOT NULL,response TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS market_trades(
                    trade_id INTEGER PRIMARY KEY,resource_id INTEGER NOT NULL,
                    price INTEGER NOT NULL,bid INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL DEFAULT 'active',item_id INTEGER NOT NULL DEFAULT 0,
                    ends_at INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS user_listings(
                    trade_id INTEGER PRIMARY KEY,item_id INTEGER NOT NULL UNIQUE,
                    starting_bid INTEGER NOT NULL,buy_now INTEGER NOT NULL,
                    ends_at INTEGER NOT NULL,state TEXT NOT NULL DEFAULT 'active');
                CREATE TABLE IF NOT EXISTS sbc_squads(
                    challenge_id INTEGER PRIMARY KEY,doc TEXT NOT NULL,
                    updated_at INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS sbc_completions(
                    challenge_id INTEGER PRIMARY KEY,times_completed INTEGER NOT NULL,
                    completed_at INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS sbc_set_completions(
                    set_id INTEGER PRIMARY KEY,times_completed INTEGER NOT NULL,
                    completed_at INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS reward_packs(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,pack_id INTEGER NOT NULL,
                    source TEXT NOT NULL,granted_at INTEGER NOT NULL);
            ''')
            self.migrate_market(db)
            for key,value in {'schema':'1','credits':'100000','clubName':'Local FC',
                              'clubAbbr':'LFC','established':str(int(time.time())),
                              'onboardingComplete':'1',
                              'activeSquadId':'1','squadName:1':'Local XI',
                              'squadFormation:1':'f442'}.items():
                db.execute('INSERT OR IGNORE INTO meta VALUES(?,?)',(key,value))

    def apply_starting_balance(self,credits,revision):
        """Apply a requested starting balance once without resetting spending."""
        marker=f'startingBalance:{revision}'
        with self.transaction() as db:
            if db.execute('SELECT 1 FROM meta WHERE key=?',(marker,)).fetchone():
                return int(db.execute("SELECT value FROM meta WHERE key='credits'").fetchone()[0])
            db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('credits',str(int(credits))))
            db.execute('INSERT INTO meta VALUES(?,?)',(marker,str(int(time.time()))))
            return int(credits)

    def purge_unsupported_womens_players(self):
        """Remove career-only women's cards accidentally imported into FUT."""
        removed=[]
        with self.transaction() as db:
            for row in db.execute('SELECT id,doc FROM items').fetchall():
                doc=json.loads(row['doc'])
                if str(doc.get('itemType','player')).lower()!='player':continue
                women=(int(doc.get('leagueId',0) or 0)==2136
                       or ' women' in str(doc.get('clubName','')).lower()
                       or ' women' in str(doc.get('leagueName','')).lower())
                if women:
                    removed.append(int(row['id']))
                    db.execute("UPDATE items SET pile='archived' WHERE id=?",(row['id'],))
            if removed:
                for squad in db.execute('SELECT id,doc FROM squads').fetchall():
                    slots=json.loads(squad['doc'])
                    if isinstance(slots,list) and any(item_id in removed for item_id in slots):
                        db.execute('UPDATE squads SET doc=? WHERE id=?',
                                   (json.dumps([0 if x in removed else x for x in slots]),
                                    squad['id']))
        return removed

    def repair_club_item_resources(self):
        """Migrate packed kit artwork IDs to FIFA's persistent FUT IDs.

        Kit art remains in assetId/cardAssetId.  The worn home/away slot uses
        6300000+team or 6400000+team; storing the art ID as resourceId made a
        selected kit fall back to the default on the next server start.
        """
        repaired=0
        with self.transaction() as db:
            for row in db.execute("SELECT id,resource_id,doc FROM items WHERE "
                                  "json_extract(doc,'$.itemType')='kit'").fetchall():
                doc=json.loads(row['doc'])
                team=int(doc.get('teamid') or doc.get('teamId') or 0)
                category=int(doc.get('category') or 0)
                if not team or category not in (2,3):continue
                resource_id=(6300000 if category==2 else 6400000)+team
                if (int(row['resource_id'])!=resource_id
                        or int(doc.get('resourceId') or 0)!=resource_id):
                    doc['resourceId']=resource_id
                    doc['definitionId']=resource_id
                    db.execute('UPDATE items SET resource_id=?,doc=? WHERE id=?',
                               (resource_id,json.dumps(doc,separators=(',',':')),
                                int(row['id'])))
                    repaired+=1
                state=str(doc.get('itemState') or '')
                if state=='activeHomeKit' and category==2:
                    db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',
                               ('homeKitId',str(resource_id)))
                elif state=='activeAwayKit' and category==3:
                    db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',
                               ('awayKitId',str(resource_id)))
        return repaired

    def repair_unique_duplicates(self):
        """Return legacy duplicate unique items to Unassigned for quick-sell.

        FUT permits a duplicate while it is in Unassigned; it must not insert
        that copy into the Club or silently archive it.  Keep the active or
        squad-seated copy and make each extra durable and sellable. Development
        cards remain stackable and are deliberately excluded.
        """
        moved=[]
        unique_types={'player','kit','custom','ball','stadium','manager'}
        with self.transaction() as db:
            squad_rows=db.execute('SELECT id,doc FROM squads').fetchall()
            seated={int(value) for row in squad_rows for value in json.loads(row['doc'])
                    if isinstance(value,int) and value>0}
            groups={}
            for row in db.execute("SELECT id,resource_id,pile,doc FROM items WHERE "
                                  "pile='club'").fetchall():
                doc=json.loads(row['doc'])
                if str(doc.get('itemType','player')).lower() not in unique_types:continue
                groups.setdefault(int(row['resource_id']),[]).append((row,doc))
            for rows in groups.values():
                if len(rows)<2:continue
                rows.sort(key=lambda pair:(
                    0 if (int(pair[0]['id']) in seated or
                          str(pair[1].get('itemState','')).startswith('active')) else 1,
                    0 if pair[0]['pile']=='club' else 1,
                    int(pair[0]['id'])))
                for row,_ in rows[1:]:
                    item_id=int(row['id']);moved.append(item_id)
                    doc=json.loads(row['doc'])
                    doc.update(itemState='free',pile=6)
                    db.execute("UPDATE items SET pile='unassigned',doc=? WHERE id=?",
                               (json.dumps(doc,separators=(',',':')),item_id))
            if moved:
                for row in squad_rows:
                    slots=json.loads(row['doc'])
                    if any(value in moved for value in slots):
                        db.execute('UPDATE squads SET doc=? WHERE id=?',
                                   (json.dumps([0 if value in moved else value
                                                for value in slots]),int(row['id'])))
        return moved

    def repair_unsupported_unassigned_club_items(self):
        """Retire pack rows which FIFA 22 cannot decode on New Items.

        The local Club Items pack used to emit a synthetic stadium definition
        (6200156).  FIFA accepts that identity in the active-squad stadium
        document, but not as a standalone pack item: it renders as a blank
        ``UNLOCK`` card and can never be redeemed.  Preserve the one equipped
        stadium and refund/retire only unassigned copies left by older builds.
        """
        removed=[]
        with self.transaction() as db:
            credits=int(db.execute("SELECT value FROM meta WHERE key='credits'").fetchone()[0])
            refund=0
            for row in db.execute(
                    "SELECT id,doc FROM items WHERE pile='unassigned' AND "
                    "json_extract(doc,'$.itemType')='stadium'").fetchall():
                doc=json.loads(row['doc'])
                item_id=int(row['id'])
                refund+=quicksell(doc)
                removed.append(item_id)
                db.execute("UPDATE items SET pile='archived' WHERE id=?",(item_id,))
            if refund:
                db.execute("UPDATE meta SET value=? WHERE key='credits'",
                           (str(credits+refund),))
        return {'itemIds':removed,'refund':refund}

    @contextmanager
    def transaction(self):
        db=sqlite3.connect(self.path,timeout=10,isolation_level=None)
        db.row_factory=sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('BEGIN IMMEDIATE')
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def import_catalogue(self,document):
        if document.get('game')!='fifa22' or not document.get('source'):
            raise StateError('Catalogue needs game=fifa22 and a source description')
        rows=document.get('players')
        if not isinstance(rows,list) or not rows:raise StateError('Catalogue must contain players')
        seen=set()
        for row in rows:
            if not isinstance(row,dict):raise StateError('Invalid player definition')
            for key in ('resourceId','assetId','rating','rareflag','nationId','teamid','leagueId'):
                if type(row.get(key)) is not int or row[key]<0:raise StateError(f'Invalid {key}')
            if row['resourceId']<1 or row['assetId']<1 or not 1<=row['rating']<=99:raise StateError('Invalid card identity/rating')
            if row.get('preferredPosition') not in ('GK','RB','RWB','CB','LB','LWB','CDM','CM','CAM','RM','LM','RW','LW','RF','LF','CF','ST'):raise StateError('Invalid position')
            if row['resourceId'] in seen:raise StateError('Duplicate resource ID')
            seen.add(row['resourceId'])
        with self.transaction() as db:
            db.execute('DELETE FROM definitions')
            for row in rows:db.execute('INSERT OR REPLACE INTO definitions VALUES(?,?)',(row['resourceId'],json.dumps(row)))
            db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('catalogueSource',str(document['source'])))
        return len(rows)

    def catalogue_source(self):
        with self.transaction() as db:
            row=db.execute("SELECT value FROM meta WHERE key='catalogueSource'").fetchone()
            return row[0] if row else None

    def get_meta(self,key,default=None):
        with self.transaction() as db:
            row=db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
            return row[0] if row else default

    def set_meta(self,key,value):
        with self.transaction() as db:
            db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',(key,str(value)))

    def rename(self,name,abbr=None):
        if not isinstance(name,str) or not name.strip() or len(name.strip())>64:
            raise StateError('Invalid club name')
        if abbr is not None and (not isinstance(abbr,str) or len(abbr.strip())>8):
            raise StateError('Invalid club abbreviation')
        with self.transaction() as db:
            changes={'clubName':name.strip(),'clubNamed':'1'}
            if abbr and abbr.strip():changes['clubAbbr']=abbr.strip()
            for key,value in changes.items():
                db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',(key,value))

    def refresh_item_definitions(self):
        """Refresh static card details after importing a better source."""
        with self.transaction() as db:
            rows=db.execute('SELECT id,resource_id,doc FROM items').fetchall()
            for item in rows:
                definition=db.execute('SELECT doc FROM definitions WHERE resource_id=?',
                                      (item['resource_id'],)).fetchone()
                if definition is None:continue
                old=json.loads(item['doc']);new=json.loads(definition['doc'])
                for key in ('contract','contracts','fitness','morale','owners','loans',
                            'untradeable','tradeable','playStyle',
                            'preferredPosition','position'):
                    if key in old:new[key]=old[key]
                if db.execute("SELECT 1 FROM market_trades WHERE item_id=? AND state='sold'",
                              (item['id'],)).fetchone():
                    new.update(untradeable=False,tradeable=True)
                db.execute('UPDATE items SET doc=? WHERE id=?',(json.dumps(new),item['id']))
            return len(rows)

    def repair_market_item_flags(self):
        """Preserve tradeability on cards bought before it was stored in doc."""
        repaired=0
        with self.transaction() as db:
            for row in db.execute("SELECT i.id,i.doc FROM items i JOIN market_trades t "
                                  "ON t.item_id=i.id WHERE t.state='sold'").fetchall():
                doc=json.loads(row['doc'])
                if doc.get('tradeable') is True and doc.get('untradeable') is False:
                    continue
                doc.update(tradeable=True,untradeable=False)
                db.execute('UPDATE items SET doc=? WHERE id=?',
                           (json.dumps(doc),row['id']))
                repaired+=1
        return repaired

    def repair_paid_pack_item_flags(self):
        """Make legacy coin-pack players tradeable.

        Older purchase responses omitted both trade flags.  The wire adapter
        intentionally defaults missing flags to untradeable, which disabled
        the transfer actions for otherwise normal Store players.  Operations
        are the durable receipt proving that an item came from a paid pack;
        reward packs and player picks remain untouched.
        """
        repaired=0
        with self.transaction() as db:
            paid_ids=set()
            for row in db.execute('SELECT request,response FROM operations').fetchall():
                try:
                    request=json.loads(row['request']);response=json.loads(row['response'])
                except (TypeError,ValueError):
                    continue
                if (not isinstance(request,dict) or request.get('reward') is not False
                        or 'player_pick' in request or not isinstance(response,dict)):
                    continue
                for item in response.get('itemData',[]):
                    if isinstance(item,dict):
                        try:paid_ids.add(int(item.get('id') or item.get('itemId') or 0))
                        except (TypeError,ValueError):pass
            for item_id in paid_ids:
                row=db.execute('SELECT doc FROM items WHERE id=?',(item_id,)).fetchone()
                if not row:continue
                doc=json.loads(row['doc'])
                if str(doc.get('itemType','player')).lower()!='player':continue
                if doc.get('tradeable') is True and doc.get('untradeable') is False:continue
                doc.update(tradeable=True,untradeable=False)
                db.execute('UPDATE items SET doc=? WHERE id=?',
                           (json.dumps(doc,separators=(',',':')),item_id))
                repaired+=1
        return repaired

    def migrate_resource_ids(self,changes):
        """Replace provisional edition IDs while keeping owned item IDs/piles."""
        moved=0
        with self.transaction() as db:
            for old_id,new_id in changes.items():
                definition=db.execute('SELECT doc FROM definitions WHERE resource_id=?',
                                      (new_id,)).fetchone()
                if definition is None:raise StateError(f'Missing replacement definition {new_id}')
                for item in db.execute('SELECT id,doc FROM items WHERE resource_id=?',
                                       (old_id,)).fetchall():
                    updated=json.loads(definition['doc'])
                    previous=json.loads(item['doc'])
                    for key in ('contract','contracts','fitness','morale','owners','loans'):
                        if key in previous:updated[key]=previous[key]
                    db.execute('UPDATE items SET resource_id=?,doc=? WHERE id=?',
                               (new_id,json.dumps(updated),item['id']))
                    moved+=1
        return moved

    def status(self):
        with self.transaction() as db:
            meta=dict(db.execute('SELECT key,value FROM meta').fetchall())
            return {'credits':int(meta['credits']),'clubName':meta['clubName'],'clubAbbr':meta['clubAbbr'],
                    'clubNamed':meta.get('clubNamed')=='1',
                    'definitions':db.execute('SELECT COUNT(*) FROM definitions').fetchone()[0],
                    'items':db.execute("SELECT COUNT(*) FROM items WHERE pile NOT IN ('market','sold','archived')").fetchone()[0]}

    def list_items(self,pile=None):
        with self.transaction() as db:
            rows=db.execute('SELECT * FROM items'+(' WHERE pile=?' if pile else " WHERE pile NOT IN ('market','sold','archived')")+' ORDER BY id',(pile,) if pile else ()).fetchall()
            # Keep the document's pile (an int the client parses). The SQLite
            # pile column is the storage bucket ('club'/'purchased') and a
            # string there made the RS4 pile getter reject the seated manager.
            out=[]
            for r in rows:
                doc=dict(json.loads(r['doc']),id=r['id'],resourceId=r['resource_id'])
                if not isinstance(doc.get('pile'),int):
                    doc['pile']=6
                out.append(doc)
            return out

    def starter_squad(self,resource_ids):
        """Grant the first club its starting XI and reserves exactly once."""
        if len(resource_ids)!=23 or len(set(resource_ids))!=23:
            raise StateError('Starter squad needs 23 distinct players')
        with self.transaction() as db:
            previous=db.execute('SELECT doc FROM squads WHERE id=1').fetchone()
            if previous:return json.loads(previous['doc'])
            if db.execute("SELECT 1 FROM items WHERE pile NOT IN ('market','sold','archived') LIMIT 1").fetchone():
                raise StateError('Existing club items need manual squad selection')
            ids=[]
            for resource_id in resource_ids:
                row=db.execute('SELECT doc FROM definitions WHERE resource_id=?',(resource_id,)).fetchone()
                if row is None:raise StateError(f'Missing starter definition {resource_id}')
                card=json.loads(row['doc'])
                cur=db.execute('INSERT INTO items(resource_id,pile,doc) VALUES(?,?,?)',
                               (resource_id,'club',json.dumps(card)))
                ids.append(cur.lastrowid)
            db.execute('INSERT INTO squads VALUES(?,?)',(1,json.dumps(ids)))
            return ids

    def grant_specials_once(self,resource_ids):
        """Put verified showcase cards in My Club without duplicating them."""
        granted=[]
        with self.transaction() as db:
            for resource_id in resource_ids:
                if db.execute("SELECT 1 FROM items WHERE resource_id=? AND pile='club'",
                              (resource_id,)).fetchone():continue
                row=db.execute('SELECT doc FROM definitions WHERE resource_id=?',
                               (resource_id,)).fetchone()
                if row is None:raise StateError(f'Missing special definition {resource_id}')
                cur=db.execute('INSERT INTO items(resource_id,pile,doc) VALUES(?,?,?)',
                               (resource_id,'club',row['doc']))
                granted.append(cur.lastrowid)
        return granted

    def ensure_manager(self,doc):
        """Own one squad manager item; the client seats it from the club.

        The match-start validation requires the manager to be an owned club
        item with a real item id, not just a card attached to the squad.
        """
        if not isinstance(doc,dict) or doc.get('itemType')!='manager':
            raise StateError('Manager doc must be a manager item')
        with self.transaction() as db:
            row=db.execute("SELECT id FROM items WHERE pile='club' AND "
                           "json_extract(doc,'$.itemType')='manager' ORDER BY id LIMIT 1").fetchone()
            if row:
                item_id=int(row[0])
            else:
                cur=db.execute('INSERT INTO items(resource_id,pile,doc) VALUES(?,?,?)',
                               (int(doc.get('resourceId') or 0),'club','{}'))
                item_id=int(cur.lastrowid)
            stored=dict(doc,id=item_id,itemId=item_id,pile=7)
            db.execute('UPDATE items SET resource_id=?,doc=? WHERE id=?',
                       (int(doc.get('resourceId') or 0),
                        json.dumps(stored,separators=(',',':')),item_id))
            return item_id

    def ensure_cosmetics(self,cards):
        """Keep the worn kit, badge and ball as owned club items.

        Squad actives refer to these same item IDs. A synthetic active with no
        owned counterpart leaves the client with an unresolved kit selection.
        """
        requested=[dict(card) for card in cards]
        with self.transaction() as db:
            # A selected kit/badge may already be a real, tradeable Club item
            # from a pack.  Seat that owned row instead of inserting a second
            # fixed-ID onboarding copy.  This is what prevents the owned copy
            # being returned to New Items as a duplicate on every restart.
            active={}
            for card in requested:
                resource_id=int(card['resourceId'])
                item_type=str(card.get('itemType','')).lower()
                candidates=[]
                for row in db.execute("SELECT id,doc FROM items WHERE pile='club' "
                                      "AND resource_id=?",(resource_id,)).fetchall():
                    stored=json.loads(row['doc'])
                    if str(stored.get('itemType','')).lower()==item_type:
                        candidates.append((row,stored))
                # Prefer a genuine pack-owned/tradeable copy, then the exact
                # requested row, then any durable matching row.
                candidates.sort(key=lambda pair:(
                    0 if pair[1].get('tradeable') is True else 1,
                    0 if int(pair[0]['id'])==int(card['id']) else 1,
                    int(pair[0]['id'])))
                if candidates:
                    row,stored=candidates[0]
                    item_id=int(row['id'])
                    merged=dict(card,id=item_id,itemId=item_id)
                    for key in ('tradeable','untradeable','discardValue','cardValue','owners'):
                        if key in stored:merged[key]=stored[key]
                    card=merged
                else:
                    item_id=int(card['id'])
                active[item_id]=card
            rows=db.execute("SELECT id,doc FROM items WHERE pile='club'").fetchall()
            for row in rows:
                if int(row['id']) in active:continue
                old=json.loads(row['doc'])
                # Retire only a synthetic onboarding duplicate of the exact
                # resource now represented by a genuine owned copy.  It must
                # not go through Unassigned or appear as a claimable item.
                replacement=next((card for card in active.values()
                                  if int(card.get('resourceId',0))==int(old.get('resourceId',0))
                                  and str(card.get('itemType','')).lower()==
                                      str(old.get('itemType','')).lower()),None)
                if (replacement and old.get('untradeable') is True
                        and old.get('tradeable') is not True
                        and str(old.get('itemState','')).startswith('active')):
                    db.execute("UPDATE items SET pile='archived' WHERE id=?",(row['id'],))
                    continue
                if old.get('itemState') not in ('activeBall','activeBadge',
                                               'activeHomeKit','activeAwayKit'):
                    continue
                old.update(itemState='free',pile=6)
                db.execute('UPDATE items SET doc=? WHERE id=?',
                           (json.dumps(old,separators=(',',':')),row['id']))
            for item_id,card in active.items():
                previous=db.execute('SELECT doc FROM items WHERE id=?',(item_id,)).fetchone()
                if previous and json.loads(previous['doc']).get('itemType') not in (
                        'kit','custom','ball','stadium'):
                    raise StateError(f'Cosmetic item ID {item_id} is already in use')
                db.execute('INSERT OR REPLACE INTO items(id,resource_id,pile,doc) '
                           'VALUES(?,?,?,?)',
                           (item_id,int(card['resourceId']),'club',
                            json.dumps(card,separators=(',',':'))))
        return list(active.values())

    def squad_ids(self):
        with self.transaction() as db:
            return [int(row[0]) for row in db.execute(
                'SELECT id FROM squads ORDER BY id').fetchall()]

    def active_squad_id(self):
        ids=self.squad_ids()
        if not ids:return 1
        try:wanted=int(self.get_meta('activeSquadId',ids[0]))
        except (TypeError,ValueError):wanted=ids[0]
        return wanted if wanted in ids else ids[0]

    def next_squad_id(self):
        ids=self.squad_ids()
        return max(ids,default=0)+1

    def squad_metadata(self,squad_id):
        squad_id=int(squad_id)
        return {'name':str(self.get_meta(f'squadName:{squad_id}',
                                         'Local XI' if squad_id==1 else f'Squad {squad_id}')),
                'formation':str(self.get_meta(f'squadFormation:{squad_id}','f442'))}

    def set_squad_metadata(self,squad_id,*,name=None,formation=None):
        squad_id=int(squad_id)
        if name is not None:
            name=str(name).strip()
            if not name or len(name)>64:raise StateError('Invalid squad name')
            self.set_meta(f'squadName:{squad_id}',name)
        if formation is not None:
            formation=str(formation).strip().lower()
            if not formation.startswith('f') or len(formation)>20:
                raise StateError('Invalid squad formation')
            self.set_meta(f'squadFormation:{squad_id}',formation)

    def set_active_squad(self,squad_id):
        squad_id=int(squad_id)
        if squad_id not in self.squad_ids():return False
        self.set_meta('activeSquadId',squad_id)
        return True

    def delete_squad(self,squad_id):
        squad_id=int(squad_id)
        with self.transaction() as db:
            count=int(db.execute('SELECT COUNT(*) FROM squads').fetchone()[0])
            if count<=1:return False
            cur=db.execute('DELETE FROM squads WHERE id=?',(squad_id,))
            if not cur.rowcount:return False
            db.execute('DELETE FROM meta WHERE key IN (?,?)',
                       (f'squadName:{squad_id}',f'squadFormation:{squad_id}'))
            active=db.execute("SELECT value FROM meta WHERE key='activeSquadId'").fetchone()
            if active and int(active[0])==squad_id:
                fallback=int(db.execute('SELECT id FROM squads ORDER BY id LIMIT 1').fetchone()[0])
                db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',
                           ('activeSquadId',str(fallback)))
            return True

    def squad(self,squad_id=None):
        # FIFA 22 uses id 0 as an active-squad alias.
        if squad_id is None or int(squad_id)==0:squad_id=self.active_squad_id()
        with self.transaction() as db:
            row=db.execute('SELECT doc FROM squads WHERE id=?',(squad_id,)).fetchone()
            return json.loads(row['doc']) if row else [0]*23

    def definitions(self,resource_ids):
        with self.transaction() as db:
            return {resource_id:json.loads(row['doc']) for resource_id in resource_ids
                    if (row:=db.execute('SELECT doc FROM definitions WHERE resource_id=?',
                                        (resource_id,)).fetchone())}

    def add_pack(self,pack):
        for key in ('id','cost','quantity','minRare','minRating'):
            if type(pack.get(key)) is not int:raise StateError('Pack parameters must be integers')
        if pack['id']<1 or pack['cost']<0 or not 1<=pack['quantity']<=100 or not 0<=pack['minRare']<=pack['quantity'] or not 1<=pack['minRating']<=99:
            raise StateError('Invalid pack parameters')
        with self.transaction() as db:db.execute('INSERT OR REPLACE INTO packs VALUES(?,?)',(pack['id'],json.dumps(pack)))

    def reward_packs(self):
        with self.transaction() as db:
            return [dict(row) for row in db.execute(
                'SELECT id,pack_id,source,granted_at FROM reward_packs ORDER BY id')]

    def grant_reward_pack(self,pack_id,count=1,source='reward'):
        """Add unopened My Packs entries without changing the coin balance."""
        pack_id=int(pack_id);count=int(count)
        if count<1:raise StateError('Reward pack count must be positive')
        with self.transaction() as db:
            if db.execute('SELECT 1 FROM packs WHERE id=?',(pack_id,)).fetchone() is None:
                raise StateError(f'Unknown reward pack {pack_id}')
            stamp=int(time.time())
            for _ in range(count):
                db.execute('INSERT INTO reward_packs(pack_id,source,granted_at) '
                           'VALUES(?,?,?)',(pack_id,str(source),stamp))

    def reward_pack_count(self,pack_id=None):
        with self.transaction() as db:
            if pack_id is None:
                return int(db.execute('SELECT COUNT(*) FROM reward_packs').fetchone()[0])
            return int(db.execute('SELECT COUNT(*) FROM reward_packs WHERE pack_id=?',
                                  (int(pack_id),)).fetchone()[0])

    def pending_player_picks(self):
        with self.transaction() as db:
            row=db.execute("SELECT value FROM meta WHERE key='pendingPlayerPicks'").fetchone()
            return json.loads(row[0]) if row else []

    @staticmethod
    def player_pick_placeholder_id(pick_id):
        return 9_500_000+int(pick_id)*10+9

    @staticmethod
    def player_pick_token(item_id,title='Player Pick'):
        """Return FIFA 22's native miscPlayerPick token, never a fake player."""
        return {'id':int(item_id),'itemId':int(item_id),
                'resourceId':PLAYER_PICK_RESOURCE_ID,
                'definitionId':PLAYER_PICK_RESOURCE_ID,
                '_definitionId':PLAYER_PICK_RESOURCE_ID,
                'assetId':PLAYER_PICK_RESOURCE_ID,
                'cardassetid':PLAYER_PICK_CARD_ASSET_ID,
                'cardsubtypeid':237,'rating':99,'rareflag':1,'rareFlag':1,
                'amount':4012,'itemType':'misc','itemState':'free','pile':6,
                'owners':1,'untradeable':True,'tradeable':False,
                'discardValue':0,'resourceGameYear':2022,
                'sourceMember':'miscPlayerPick','name':'PlayerPickItemName113',
                'displayName':str(title),'shortName':str(title),
                'itemName':str(title),
                'detaildescription':'PlayerPickItemDetailDesc113',
                'description':str(title),'title':str(title)}

    def _insert_player_pick_placeholder(self,db,pick):
        item_id=self.player_pick_placeholder_id(pick['id'])
        options=max(2,len(pick.get('choices',[])))
        title=str(pick.get('title') or f'1 of {options} Promo Players')
        card=self.player_pick_token(item_id,title)
        card['pickPlaceholder']=int(pick['id'])
        db.execute('INSERT OR REPLACE INTO items(id,resource_id,pile,doc) VALUES(?,?,?,?)',
                   (item_id,PLAYER_PICK_RESOURCE_ID,'unassigned',json.dumps(card)))

    def sync_player_pick_placeholders(self):
        with self.transaction() as db:
            row=db.execute("SELECT value FROM meta WHERE key='pendingPlayerPicks'").fetchone()
            for pick in json.loads(row[0]) if row else []:
                self._insert_player_pick_placeholder(db,pick)

    def open_player_pick(self,item_id):
        with self.transaction() as db:
            row=db.execute("SELECT value FROM meta WHERE key='pendingPlayerPicks'").fetchone()
            pending=json.loads(row[0]) if row else []
            pick=next((entry for entry in pending if
                       self.player_pick_placeholder_id(entry['id'])==int(item_id)),None)
            if pick is None:raise StateError('Unknown player pick item')
            for entry in pending:entry.pop('opened',None)
            pick['opened']=True
            db.execute("INSERT OR REPLACE INTO meta VALUES('pendingPlayerPicks',?)",
                       (json.dumps(pending),))
            return pick

    def grant_player_pick(self,choices,operation_id,title='Player Pick'):
        ids=[int(value) for value in choices]
        if not 2<=len(ids)<=5 or len(ids)!=len(set(ids)):
            raise StateError('Player pick needs distinct cards')
        with self.transaction() as db:
            old=db.execute('SELECT response FROM operations WHERE id=?',
                           (operation_id,)).fetchone()
            if old:return json.loads(old['response'])
            for resource_id in ids:
                if not db.execute('SELECT 1 FROM definitions WHERE resource_id=?',
                                  (resource_id,)).fetchone():
                    raise StateError(f'Unknown player pick card {resource_id}')
            row=db.execute("SELECT value FROM meta WHERE key='pendingPlayerPicks'").fetchone()
            pending=json.loads(row[0]) if row else []
            seq=int(db.execute("SELECT COALESCE((SELECT value FROM meta WHERE key='nextPlayerPickId'),'1')").fetchone()[0])
            pick={'id':seq,'choices':ids,'title':str(title)}
            pending.append(pick);self._insert_player_pick_placeholder(db,pick)
            db.execute("INSERT OR REPLACE INTO meta VALUES('pendingPlayerPicks',?)",
                       (json.dumps(pending),))
            db.execute("INSERT OR REPLACE INTO meta VALUES('nextPlayerPickId',?)",
                       (str(seq+1),))
            db.execute('INSERT INTO operations VALUES(?,?,?)',
                       (operation_id,json.dumps({'choices':ids}),json.dumps(pick)))
            return pick

    def purchase_player_pick(self,pack_id,choices,operation_id,title):
        """Charge once and grant exactly one native Player Pick token."""
        ids=[int(value) for value in choices]
        if not 2<=len(ids)<=5 or len(ids)!=len(set(ids)):
            raise StateError('Player pick needs distinct cards')
        if not isinstance(operation_id,str) or not 1<=len(operation_id)<=128:
            raise StateError('A bounded operation ID is required')
        request=json.dumps({'pack_id':int(pack_id),'player_pick':ids},sort_keys=True)
        with self.transaction() as db:
            previous=db.execute('SELECT request,response FROM operations WHERE id=?',
                                (operation_id,)).fetchone()
            if previous:
                if previous['request']!=request:
                    raise StateError('Operation ID already used for a different request')
                return json.loads(previous['response'])
            saved=db.execute('SELECT doc FROM packs WHERE id=?',(int(pack_id),)).fetchone()
            if saved is None:raise StateError('Unknown pack')
            pack=json.loads(saved['doc'])
            credits=int(db.execute("SELECT value FROM meta WHERE key='credits'").fetchone()[0])
            if credits<int(pack['cost']):raise StateError('Insufficient coins')
            for resource_id in ids:
                if not db.execute('SELECT 1 FROM definitions WHERE resource_id=?',
                                  (resource_id,)).fetchone():
                    raise StateError(f'Unknown player pick card {resource_id}')
            row=db.execute("SELECT value FROM meta WHERE key='pendingPlayerPicks'").fetchone()
            pending=json.loads(row[0]) if row else []
            seq=int(db.execute("SELECT COALESCE((SELECT value FROM meta WHERE key='nextPlayerPickId'),'1')").fetchone()[0])
            pick={'id':seq,'choices':ids,'title':str(title)}
            pending.append(pick);self._insert_player_pick_placeholder(db,pick)
            db.execute("INSERT OR REPLACE INTO meta VALUES('pendingPlayerPicks',?)",
                       (json.dumps(pending),))
            db.execute("INSERT OR REPLACE INTO meta VALUES('nextPlayerPickId',?)",
                       (str(seq+1),))
            credits-=int(pack['cost'])
            db.execute("UPDATE meta SET value=? WHERE key='credits'",(str(credits),))
            token_id=self.player_pick_placeholder_id(seq)
            result={'credits':credits,'itemData':[self.player_pick_token(token_id,title)],
                    'duplicateItemIdList':[],'pick':pick,'rewardPack':False}
            db.execute('INSERT INTO operations VALUES(?,?,?)',
                       (operation_id,request,json.dumps(result)))
            return result

    def resolve_player_pick(self,resource_id):
        with self.transaction() as db:
            row=db.execute("SELECT value FROM meta WHERE key='pendingPlayerPicks'").fetchone()
            pending=json.loads(row[0]) if row else []
            if not pending:
                old=db.execute("SELECT value FROM meta WHERE key='lastPlayerPickSelection'").fetchone()
                if old:
                    previous=json.loads(old[0])
                    if int(previous['resourceId'])==int(resource_id):return previous
                raise StateError('No pending player pick')
            pick=next((entry for entry in pending if entry.get('opened')),pending[0])
            ids=[int(value) for value in pick['choices']]
            if int(resource_id) not in ids:raise StateError('Card is not in the pending pick')
            index=ids.index(int(resource_id));item_id=9_500_000+int(pick['id'])*10+index
            definition=db.execute('SELECT doc FROM definitions WHERE resource_id=?',
                                  (int(resource_id),)).fetchone()
            if not definition:raise StateError('Missing chosen card definition')
            card=json.loads(definition['doc'])
            card.update(id=item_id,itemId=item_id,pile=6,itemType='player',
                        untradeable=True,tradeable=False,owners=1,
                        playerPickReward=True)
            db.execute('INSERT OR IGNORE INTO items(id,resource_id,pile,doc) VALUES(?,?,?,?)',
                       (item_id,int(resource_id),'unassigned',json.dumps(card)))
            pending.remove(pick)
            db.execute('DELETE FROM items WHERE id=? AND pile=?',
                       (self.player_pick_placeholder_id(pick['id']),'unassigned'))
            db.execute("INSERT OR REPLACE INTO meta VALUES('pendingPlayerPicks',?)",
                       (json.dumps(pending),))
            db.execute("INSERT OR REPLACE INTO meta VALUES('lastPlayerPickSelection',?)",
                       (json.dumps(card),))
            return card

    def commit_objective_claim(self,state,reward,source):
        """Persist one Season reward and its claim marker atomically."""
        reward_type,value,count=reward
        value=int(value);count=max(1,int(count))
        with self.transaction() as db:
            if reward_type=='coin':
                current=int(db.execute(
                    "SELECT value FROM meta WHERE key='credits'").fetchone()[0])
                db.execute("UPDATE meta SET value=? WHERE key='credits'",
                           (str(current+value*count),))
            elif reward_type=='pack':
                if db.execute('SELECT 1 FROM packs WHERE id=?',(value,)).fetchone() is None:
                    raise StateError(f'Unknown Objectives reward pack {value}')
                stamp=int(time.time())
                for _ in range(count):
                    db.execute('INSERT INTO reward_packs(pack_id,source,granted_at) '
                               'VALUES(?,?,?)',(value,str(source),stamp))
            elif reward_type!='xp':
                raise StateError(f'Unsupported Objectives reward {reward_type}')
            db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',
                       ('scmpCampaignFut22V1',
                        json.dumps(state,separators=(',',':'))))

    @staticmethod
    def _sbc_entries(payload,squad_size=None):
        root=payload.get('squad',payload) if isinstance(payload,dict) else {}
        players=root.get('players',[]) if isinstance(root,dict) else []
        result=[]
        seen=set()
        for fallback_index,entry in enumerate(players if isinstance(players,list) else []):
            if not isinstance(entry,dict):continue
            raw_index=entry.get('index',fallback_index)
            try:index=int(raw_index)
            except (TypeError,ValueError):continue
            # FIFA sends all 23 squad-editor slots, including reserves and
            # BRICK placeholders. SBC requirements only address the leading
            # challenge slots; accepting the rest caused a 409 and discarded
            # an otherwise valid Squad Builder result.
            if index<0 or (squad_size is not None and index>=int(squad_size)):
                continue
            item=entry.get('itemData') if isinstance(entry.get('itemData'),dict) else entry
            raw=item.get('id',item.get('itemId',entry.get('itemId',0)))
            try:item_id=int(raw)
            except (TypeError,ValueError):continue
            if item_id<=0 or item_id in seen:continue
            seen.add(item_id);result.append((index,item_id))
        return result

    @classmethod
    def _sbc_item_ids(cls,payload,squad_size=None):
        return [item_id for _,item_id in cls._sbc_entries(payload,squad_size)]

    def save_sbc_squad(self,challenge_id,payload,squad_size):
        entries=self._sbc_entries(payload,squad_size)
        item_ids=[item_id for _,item_id in entries]
        chemistry=-1
        if isinstance(payload,dict):
            root=payload.get('squad',payload)
            if isinstance(root,dict) and str(root.get('chemistry','')).lstrip('-').isdigit():
                chemistry=int(root['chemistry'])
        with self.transaction() as db:
            for item_id in item_ids:
                row=db.execute("SELECT doc FROM items WHERE id=? AND pile='club'",
                               (item_id,)).fetchone()
                if row is None or str(json.loads(row['doc']).get('itemType','player')).lower()!='player':
                    raise StateError('SBC entries must be owned players')
            doc={'players':[{'index':index,'itemData':{'id':item_id}}
                            for index,item_id in entries],
                 'chemistry':chemistry}
            db.execute('INSERT OR REPLACE INTO sbc_squads VALUES(?,?,?)',
                       (int(challenge_id),json.dumps(doc,separators=(',',':')),
                        int(time.time())))
        return doc

    def sbc_squad(self,challenge_id):
        with self.transaction() as db:
            row=db.execute('SELECT doc FROM sbc_squads WHERE challenge_id=?',
                           (int(challenge_id),)).fetchone()
            return json.loads(row['doc']) if row else None

    def sbc_completion_counts(self):
        with self.transaction() as db:
            challenges={int(row['challenge_id']):int(row['times_completed'])
                        for row in db.execute('SELECT * FROM sbc_completions')}
            sets={int(row['set_id']):int(row['times_completed'])
                  for row in db.execute('SELECT * FROM sbc_set_completions')}
            return challenges,sets

    def complete_sbc(self,challenge_id,set_id,item_ids,challenge_awards,
                     set_challenge_ids,set_awards,repeatable=False):
        """Consume an SBC squad and grant coins/My Packs atomically."""
        item_ids=list(dict.fromkeys(int(value) for value in item_ids))
        stamp=int(time.time())
        granted=[];set_completed=False
        with self.transaction() as db:
            prior=db.execute('SELECT times_completed FROM sbc_completions WHERE challenge_id=?',
                             (int(challenge_id),)).fetchone()
            prior_count=int(prior['times_completed']) if prior else 0
            if prior_count and not repeatable:
                return {'alreadyCompleted':True,'setCompleted':False,
                        'consumed':[],'awards':[]}
            for item_id in item_ids:
                row=db.execute("SELECT doc FROM items WHERE id=? AND pile='club'",
                               (item_id,)).fetchone()
                if row is None or str(json.loads(row['doc']).get('itemType','player')).lower()!='player':
                    raise StateError('SBC player is no longer available')
            for item_id in item_ids:
                db.execute("UPDATE items SET pile='archived' WHERE id=?",(item_id,))
            for squad in db.execute('SELECT id,doc FROM squads').fetchall():
                slots=json.loads(squad['doc'])
                if isinstance(slots,list) and any(value in item_ids for value in slots):
                    db.execute('UPDATE squads SET doc=? WHERE id=?',
                               (json.dumps([0 if value in item_ids else value for value in slots]),
                                int(squad['id'])))
            db.execute('INSERT INTO sbc_completions VALUES(?,?,?) '
                       'ON CONFLICT(challenge_id) DO UPDATE SET '
                       'times_completed=excluded.times_completed,'
                       'completed_at=excluded.completed_at',
                       (int(challenge_id),prior_count+1,stamp))
            db.execute('DELETE FROM sbc_squads WHERE challenge_id=?',(int(challenge_id),))

            def grant(awards,source):
                nonlocal granted
                for award in awards:
                    count=max(1,int(award.get('count',1) or 1))
                    kind=str(award.get('type','')).upper()
                    if kind=='COINS':
                        value=max(0,int(award.get('value',0) or 0))*count
                        current=int(db.execute("SELECT value FROM meta WHERE key='credits'").fetchone()[0])
                        db.execute("UPDATE meta SET value=? WHERE key='credits'",
                                   (str(current+value),))
                        granted.append(dict(award,value=value))
                    elif kind=='PACK':
                        pack_id=int(award.get('packId',award.get('pack_id',0)) or 0)
                        if db.execute('SELECT 1 FROM packs WHERE id=?',(pack_id,)).fetchone() is None:
                            raise StateError(f'Unknown SBC reward pack {pack_id}')
                        for _ in range(count):
                            db.execute('INSERT INTO reward_packs(pack_id,source,granted_at) VALUES(?,?,?)',
                                       (pack_id,source,stamp))
                        granted.append(dict(award,packId=pack_id,count=count))

            grant(challenge_awards,f'SBC challenge {challenge_id}')
            challenge_cycles=[]
            for cid in set_challenge_ids:
                row=db.execute('SELECT times_completed FROM sbc_completions '
                               'WHERE challenge_id=?',(int(cid),)).fetchone()
                challenge_cycles.append(int(row['times_completed']) if row else 0)
            completed_cycles=min(challenge_cycles) if challenge_cycles else 0
            set_row=db.execute('SELECT times_completed FROM sbc_set_completions '
                               'WHERE set_id=?',(int(set_id),)).fetchone()
            prior_cycles=int(set_row['times_completed']) if set_row else 0
            if completed_cycles>prior_cycles:
                db.execute('INSERT INTO sbc_set_completions VALUES(?,?,?) '
                           'ON CONFLICT(set_id) DO UPDATE SET '
                           'times_completed=excluded.times_completed,'
                           'completed_at=excluded.completed_at',
                           (int(set_id),completed_cycles,stamp))
                grant(set_awards,f'SBC set {set_id}')
                set_completed=True
        return {'alreadyCompleted':False,'setCompleted':set_completed,
                'timesCompleted':prior_count+1,
                'consumed':item_ids,'awards':granted}

    def purchase(self,pack_id,operation_id,force_ids=None,reward=False):
        if not isinstance(operation_id,str) or not 1<=len(operation_id)<=128:raise StateError('A bounded operation ID is required')
        request=json.dumps({'pack_id':pack_id,'forced':sorted(force_ids or []),
                            'reward':bool(reward)},sort_keys=True)
        with self.transaction() as db:
            previous=db.execute('SELECT request,response FROM operations WHERE id=?',(operation_id,)).fetchone()
            if previous:
                if previous['request']!=request:raise StateError('Operation ID already used for a different request')
                return json.loads(previous['response'])
            saved=db.execute('SELECT doc FROM packs WHERE id=?',(pack_id,)).fetchone()
            if not saved:raise StateError('Unknown pack')
            pack=json.loads(saved['doc'])
            reward_row=None
            if reward:
                reward_row=db.execute('SELECT id FROM reward_packs WHERE pack_id=? '
                                      'ORDER BY id LIMIT 1',(int(pack_id),)).fetchone()
                if reward_row is None:raise StateError('Unopened reward pack not found')
            credits=int(db.execute("SELECT value FROM meta WHERE key='credits'").fetchone()[0])
            if not reward and credits<pack['cost']:raise StateError('Insufficient coins')
            item_pool=list(pack.get('itemPool') or [])
            unique_types={'player','kit','custom','ball','stadium','manager'}
            club_unique_types={'kit','custom','ball','stadium','manager'}
            owned_unique={int(row[0]) for row in db.execute(
                "SELECT resource_id FROM items WHERE pile IN ('club','unassigned')"
            ).fetchall()}
            if force_ids is not None and item_pool:
                by_resource={int(row['resourceId']):row for row in item_pool}
                cards=[dict(by_resource[int(resource_id)]) for resource_id in
                       force_ids[:pack['quantity']] if int(resource_id) in by_resource]
                if len(cards)<pack['quantity']:
                    raise StateError('Preview roll cannot satisfy this pack')
            elif force_ids is not None:
                # The preview flow is authoritative: the cards the client was
                # shown are the cards this pack delivers.
                cards=[]
                for resource_id in force_ids[:pack['quantity']]:
                    row=db.execute('SELECT doc FROM definitions WHERE resource_id=?',(int(resource_id),)).fetchone()
                    if row is None:raise StateError(f'Missing forced definition {resource_id}')
                    cards.append(json.loads(row[0]))
                if len(cards)<pack['quantity']:raise StateError('Preview roll cannot satisfy this pack')
            elif item_pool:
                rng=random.SystemRandom()
                pool=[dict(card) for card in item_pool]
                if all(str(card.get('itemType','player')).lower() in
                       club_unique_types for card in pool):
                    # Existing club cosmetics are valid duplicate pack items.
                    # Keep the copy Unassigned and report it for quick-sell.
                    if len(pool)<pack['quantity']:
                        raise StateError('Pack has too few club items')
                    cards=rng.sample(pool,pack['quantity'])
                elif all(str(card.get('itemType','player')).lower() in unique_types
                         for card in pool):
                    # Players may be packed again; the new copy is correctly
                    # reported as a duplicate in Unassigned.  Filtering owned
                    # definitions here made whole promo families disappear as
                    # the club grew. Other unique item pools retain their
                    # ownership filter.
                    if not all(str(card.get('itemType','player')).lower()=='player'
                               for card in pool):
                        pool=[card for card in pool
                              if int(card['resourceId']) not in owned_unique]
                    if len(pool)<pack['quantity']:
                        raise StateError('Pack has too few unique items')
                    cards=rng.sample(pool,pack['quantity'])
                else:
                    # Development cards are intentionally stackable. Contracts,
                    # chemistry styles and position modifiers may repeat.
                    cards=[dict(rng.choice(pool)) for _ in range(pack['quantity'])]
            else:
                pool=[json.loads(r[0]) for r in db.execute('SELECT doc FROM definitions')]
                promo_only=pack.get('pool')=='special'
                if promo_only:pool=[p for p in pool if p.get('special')]
                elif pack.get('pool')=='base_common':
                    pool=[p for p in pool if not p.get('special')
                          and int(p.get('rareflag',0) or 0)==0]
                elif pack.get('group')!='gold':
                    pool=[p for p in pool if not p.get('special')]
                pool=[p for p in pool if p['rating']>=pack['minRating']
                      and p['rating']<=int(pack.get('maxRating',99))]
                base=[p for p in pool if not p.get('special')]
                specials=[p for p in pool if p.get('special')]
                rare=[p for p in base if p['rareflag']>0]
                if (len(pool)<pack['quantity'] or
                        (not promo_only and
                         len(rare)<min(pack['minRare'],pack['quantity']))):
                    raise StateError('Catalogue cannot satisfy this pack')
                rng=random.SystemRandom()
                cards=[];selected=set();family_counts={}
                def promo_card():
                    by_family={}
                    for candidate in specials:
                        if int(candidate['resourceId']) in selected:continue
                        family=str(candidate.get('specialVersion') or
                                   candidate.get('promo') or
                                   candidate.get('rareflag') or 'special')
                        by_family.setdefault(family,[]).append(candidate)
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
                    choice=promo_card() if roll_promo else None
                    if choice is None:
                        source=rare if index<int(pack['minRare']) else base
                        candidates=[candidate for candidate in source
                                    if int(candidate['resourceId']) not in selected]
                        if not candidates:
                            choice=promo_card()
                            if choice is None:
                                raise StateError('Catalogue cannot satisfy this pack')
                        else:choice=rng.choice(candidates)
                    cards.append(choice);selected.add(int(choice['resourceId']))
            item_data=[]
            duplicates=[]
            for card in cards:
                card=dict(card)
                item_type=str(card.get('itemType','player')).lower()
                # Coin Store players are tradeable.  Explicit flags are
                # required because FIFA disables squad transfer actions when
                # either field is absent. Reward packs retain their source
                # policy and player picks use their separate untradeable path.
                if item_type=='player' and not reward:
                    card.update(tradeable=True,untradeable=False)
                existing=(db.execute("SELECT id FROM items WHERE resource_id=? "
                                     "AND pile='club' ORDER BY id LIMIT 1",
                                     (card['resourceId'],)).fetchone()
                          if item_type in unique_types else None)
                cur=db.execute('INSERT INTO items(resource_id,pile,doc) VALUES(?,?,?)',(card['resourceId'],'unassigned',json.dumps(card)))
                item_data.append(dict(card,id=cur.lastrowid,pile=6,
                                      itemType=card.get('itemType','player')))
                if existing:duplicates.append({'itemId':cur.lastrowid,'duplicateItemId':existing['id']})
            if not reward:credits-=pack['cost']
            db.execute("UPDATE meta SET value=? WHERE key='credits'",(str(credits),))
            if reward_row is not None:
                db.execute('DELETE FROM reward_packs WHERE id=?',(int(reward_row['id']),))
            result={'credits':credits,'itemData':item_data,
                    'duplicateItemIdList':duplicates,'rewardPack':bool(reward)}
            db.execute('INSERT INTO operations VALUES(?,?,?)',(operation_id,request,json.dumps(result)))
            return result

    def move(self,item_ids,pile):
        if pile not in ('club','unassigned','trade'):raise StateError('Invalid pile')
        if not isinstance(item_ids,list) or not item_ids or len(item_ids)!=len(set(item_ids)) or any(type(i) is not int or i<1 for i in item_ids):raise StateError('Invalid item IDs')
        with self.transaction() as db:
            for item_id in item_ids:
                row=db.execute('SELECT * FROM items WHERE id=?',(item_id,)).fetchone()
                if row is None:raise StateError('Unknown item')
                if row['pile'] not in ('club','unassigned','trade'):raise StateError('Item is not owned')
                self._market_settle_due(db)
                if db.execute("SELECT 1 FROM user_listings WHERE item_id=? AND state IN ('active','closed','cleared')",(item_id,)).fetchone():
                    raise StateError('Listed or sold item cannot be moved')
                doc=json.loads(row['doc'])
                item_type=str(doc.get('itemType','player')).lower()
                duplicate=(pile=='club'
                           and item_type in {'player','kit','custom','ball',
                                             'stadium','manager'}
                           and db.execute("SELECT 1 FROM items WHERE resource_id=? "
                                          "AND pile='club' AND id!=?",
                                          (row['resource_id'],item_id)).fetchone())
                if duplicate:
                    raise StateError('Duplicate unique item already in club')
                db.execute('UPDATE items SET pile=? WHERE id=?',(pile,item_id))
                if pile!='trade':
                    db.execute("UPDATE user_listings SET state='closed' WHERE "
                               "item_id=? AND state='active'",(item_id,))
                if pile!='club':
                    for squad in db.execute('SELECT id,doc FROM squads').fetchall():
                        slots=json.loads(squad['doc'])
                        if item_id in slots:
                            db.execute('UPDATE squads SET doc=? WHERE id=?',(json.dumps([0 if x==item_id else x for x in slots]),squad['id']))
        return {'moved':item_ids,'pile':pile}

    def update_item_fields(self,item_id,fields):
        """Persist a consumable mutation on one owned player/manager."""
        with self.transaction() as db:
            row=db.execute("SELECT * FROM items WHERE id=? AND pile='club'",
                           (int(item_id),)).fetchone()
            if row is None:return None
            doc=json.loads(row['doc']);doc.update(dict(fields))
            db.execute('UPDATE items SET doc=? WHERE id=?',
                       (json.dumps(doc,separators=(',',':')),int(item_id)))
            return dict(doc,id=int(item_id),resourceId=int(row['resource_id']))

    def consume_resource(self,resource_id):
        """Consume exactly one persistent development card instance."""
        with self.transaction() as db:
            row=db.execute("SELECT id FROM items WHERE resource_id=? AND pile='club' "
                           "AND json_extract(doc,'$.itemType')='development' "
                           "ORDER BY id LIMIT 1",(int(resource_id),)).fetchone()
            if row is None:return False
            db.execute("UPDATE items SET pile='archived' WHERE id=?",(int(row['id']),))
            return True

    def activate_club_item(self,item_id,item_state):
        """Persist one equipped cosmetic per native active slot."""
        allowed={'activeHomeKit','activeAwayKit','activeBadge','activeBall',
                 'activeStadium'}
        with self.transaction() as db:
            row=db.execute("SELECT * FROM items WHERE id=? AND pile='club'",
                           (int(item_id),)).fetchone()
            if row is None:raise StateError('Club item is not owned')
            doc=json.loads(row['doc'])
            item_type=str(doc.get('itemType','')).lower()
            # FIFA sends itemState="free"/"active" without a slot on some
            # Customize paths.  Match the proven FIFA 20 behavior and infer
            # the durable active slot from the item's native type/category.
            if item_state not in allowed:
                if item_type=='kit':
                    category=int(doc.get('category') or 0)
                    item_state=('activeHomeKit' if category==2 else
                                'activeAwayKit' if category==3 else '')
                elif item_type=='custom':item_state='activeBadge'
                elif item_type=='ball':item_state='activeBall'
                elif item_type=='stadium':item_state='activeStadium'
            if item_state not in allowed:raise StateError('Invalid active club slot')
            expected={'activeHomeKit':'kit','activeAwayKit':'kit',
                      'activeBadge':'custom','activeBall':'ball',
                      'activeStadium':'stadium'}[item_state]
            if item_type!=expected:
                raise StateError('Club item does not fit active slot')
            if expected=='kit':
                category=int(doc.get('category') or 0)
                if ((item_state=='activeHomeKit' and category!=2)
                        or (item_state=='activeAwayKit' and category!=3)):
                    raise StateError('Kit does not fit home/away slot')
                team=int(doc.get('teamid') or doc.get('teamId') or 0)
                if not team:raise StateError('Kit has no team identity')
                resource_id=(6300000 if category==2 else 6400000)+team
                doc['resourceId']=resource_id
                doc['definitionId']=resource_id
            else:
                resource_id=int(row['resource_id'])
            for other in db.execute("SELECT id,doc FROM items WHERE pile='club'").fetchall():
                other_doc=json.loads(other['doc'])
                if other_doc.get('itemState')==item_state:
                    other_doc.update(itemState='free',pile=6)
                    db.execute('UPDATE items SET doc=? WHERE id=?',
                               (json.dumps(other_doc,separators=(',',':')),
                                int(other['id'])))
            doc.update(itemState=item_state,pile=7)
            db.execute('UPDATE items SET resource_id=?,doc=? WHERE id=?',
                       (resource_id,json.dumps(doc,separators=(',',':')),
                        int(item_id)))
            meta_key={'activeHomeKit':'homeKitId','activeAwayKit':'awayKitId',
                      'activeBadge':'badgeId','activeBall':'ballId',
                      'activeStadium':'stadiumId'}[item_state]
            db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',
                       (meta_key,str(resource_id)))
            return dict(doc,id=int(item_id),resourceId=resource_id)

    def discard_items(self,item_ids):
        """Quick-sell owned cards atomically and return the new balance."""
        if not isinstance(item_ids,list) or any(type(i) is not int or i<1
                                                for i in item_ids):
            raise StateError('Invalid discard item IDs')
        item_ids=list(dict.fromkeys(item_ids))
        with self.transaction() as db:
            self._market_settle_due(db)
            discarded=[];total=0
            for item_id in item_ids:
                row=db.execute('SELECT * FROM items WHERE id=?',(item_id,)).fetchone()
                if row is None:
                    # A retry after a successful discard must not pay twice.
                    continue
                if row['pile']=='archived':
                    continue
                if row['pile'] not in ('club','unassigned'):
                    raise StateError('Item is not available for discard')
                if db.execute("SELECT 1 FROM user_listings WHERE item_id=? AND state='active'",
                              (item_id,)).fetchone():
                    raise StateError('Listed item cannot be discarded')
                card=json.loads(row['doc'])
                if card.get('undiscardable',False):
                    raise StateError('Item cannot be discarded')
                total+=quicksell(card);discarded.append(item_id)
                db.execute("UPDATE items SET pile='archived' WHERE id=?",(item_id,))
                for squad in db.execute('SELECT id,doc FROM squads').fetchall():
                    slots=json.loads(squad['doc'])
                    if item_id in slots:
                        db.execute('UPDATE squads SET doc=? WHERE id=?',
                                   (json.dumps([0 if x==item_id else x for x in slots]),
                                    squad['id']))
            credits=self._coins(db,total)
        return {'discarded':discarded,'value':total,'credits':credits}

    def save_squad(self,squad_id,slots):
        if type(squad_id) is not int or squad_id<0 or not isinstance(slots,list) or len(slots)!=23:raise StateError('Squad needs 23 slots')
        if squad_id==0:squad_id=self.active_squad_id()
        if any(type(i) is not int or i<0 for i in slots):raise StateError('Invalid squad item ID')
        ids=[i for i in slots if i]
        if len(ids)!=len(set(ids)):raise StateError('Same item in more than one slot')
        with self.transaction() as db:
            players=set()
            for item_id in ids:
                item=db.execute("SELECT resource_id,doc FROM items WHERE id=? AND pile='club'",(item_id,)).fetchone()
                if not item:raise StateError('Squad item must belong to club')
                card=json.loads(item['doc'])
                identity=presented_asset(item['resource_id'],card.get('assetId',item['resource_id']))
                if identity in players:raise StateError('Same player in more than one slot')
                players.add(identity)
            db.execute('INSERT OR REPLACE INTO squads VALUES(?,?)',(squad_id,json.dumps(slots)))
            db.execute('INSERT OR IGNORE INTO meta VALUES(?,?)',
                       (f'squadName:{squad_id}',
                        'Local XI' if squad_id==1 else f'Squad {squad_id}'))
            db.execute('INSERT OR IGNORE INTO meta VALUES(?,?)',
                       (f'squadFormation:{squad_id}','f442'))
        return {'id':squad_id,'slots':slots}

    def club_players(self,filters=None):
        """Honour the squad picker exclusions; general My Club still owns cards."""
        q=query_values(filters)
        for key in ('exclitem','exclid','exclude','excldef'):
            if key in (filters or {}):q[key]=filters[key]
        def ids(*keys):
            found=set()
            for key in keys:
                raw=q.get(key,'')
                for value in (raw if isinstance(raw,list) else str(raw).split(',')):
                    if str(value).strip().isdigit():found.add(int(value))
            return found
        excluded=ids('exclitem','exclid','exclude')
        definitions=ids('excldef')
        items=[i for i in self.list_items('club') if i.get('itemType','player')=='player']
        identities={presented_asset(i['resourceId'],i.get('assetId',i['resourceId'])) for i in items if i['id'] in excluded}
        return [i for i in items if i['id'] not in excluded and i['resourceId'] not in definitions
                and presented_asset(i['resourceId'],i.get('assetId',i['resourceId'])) not in identities
                and matches(i,q)]

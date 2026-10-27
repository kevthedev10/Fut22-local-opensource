"""Import the user-supplied FIFA 22 player roster with explicit provenance.

This is the game's career roster, used as a bootstrap for FUT base cards.
Special FUT card editions are outside this source and remain unverified.
"""
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
CSV=ROOT/'data/players_22.csv'
ARCHIVE=ROOT/'data/players_fifa22.json'
SPECIALS=ROOT/'data/futbin22_specials.json'
SUPPLIED_SPECIALS=ROOT/'data/fut22_specials.json'
LINKS=ROOT/'data/leagueteamlinks.json'
SOURCE='User-supplied asdas.rar FIFA 22 male FUT catalogue; supplied fut22_specials.json editions with exact promo-color families, shell-mapped rarities and ICON base promotion; FUTBIN verified featured IDs; card art IDs 5/11; catalogue revision 2'
FALLBACK_SOURCE='FIFA 22 male career roster (MakuG/fifa22 players_22.csv); league IDs cross-checked against supplied FIFA 20 leagueteamlinks; FUT rarity inferred'
POSITIONS={'GK','RB','RWB','CB','LB','LWB','CDM','CM','CAM','RM','LM','RW','LW','RF','LF','CF','ST'}
# Promo design -> shell (rarity id), from the supplied fifa22work mapping
# (fut22_promo_rarity.json), which was matched against the card-background
# files in the user's own content tree. The background filenames carry the
# rarity id in DECIMAL - cards_bg_s_1_12_0.dds is the ICON shell - and every
# id below has art in all four sizes under references/asdas/.../itemBGs.
PROMO_RARITY={'ucl_live':124,'legend':12,'prime_icon_moments':12,
              'player_moments':90,'toty':5,'toty_nominees':64,
              'futties_winner':16,'futties_premium':174,'shapeshifters':97,
              'totw_gold':3,'totw_silver':3,'tots_gold':11,
              'fut_future_stars':71,'fut_birthday':30,'motm':8,
              'award_winner':110,'fut_fantasy_red':136,'sbc_base':24,
              'winter_wildcard':118,'ones_to_watch':21,'purple':4,'pink':9,
              'fut_heroes':172,'shapeshifters_heroes':173,'headliners':95,
              'fut_captains':137,'fut_versus_fire':116,'fut_versus_ice':108,
              'fut_showdown':58,'fut_showdown_boost':57,'signature_signings':110,
              'sbc_premium':25,'sbc_flashback':51,'objectives_reward':91,
              'record_breaker':6,'halloween':22,'ucl_tott':70,'ucl_rttf':124,
              'ucl_motm':49,'europa_live':46,'europa_rttf':125,'europa_motm':68,
              'europa_conference_league':104,'europa_conference_league_3':105,
              'europa_conference_league_motm':126,'libertadores_totgs':143,
              'potm_pl':43,'ligue_1_potm':79,'potm_serie_a':114,
              'potm_eredivisie':115,'la_liga_potm':86,'tots_moments':11,
              'adidas':107,'fut_captains_upgrade':137,
              'fut_fantasy_blue':136,'headliners_upgrade':95,
              'objectives_reward_2':91,'silver_special':130,
              'rare_gold':1,'rare_silver':1}

def _num(value):
    try:return int(float(value))
    except (ValueError,TypeError):return 0

def _is_womens_player(row):
    """FIFA 22 FUT did not include women's cards in its player database.

    The supplied career archive includes the international women's league.
    Keeping those rows in local FUT also selects unsupported heads/models in
    Draft and is one source of the blank/mismatched in-match portraits.
    """
    return (_num(row.get('leagueId',row.get('league_id'))) == 2136
            or ' women' in str(row.get('teamName',row.get('clubName',''))).lower()
            or ' women' in str(row.get('leagueName',row.get('league_name',''))).lower())

def load_catalogue():
    if ARCHIVE.is_file():
        raw=json.loads(ARCHIVE.read_text(encoding='utf-8'))
        players=[]
        for key,row in raw.items():
            asset=_num(row.get('assetId'))
            if _num(key)!=asset or not 1<=_num(row.get('rating'))<=99:continue
            if _is_womens_player(row):continue
            pos=row.get('preferredPosition')
            if pos not in POSITIONS:continue
            players.append({'resourceId':asset,'assetId':asset,'rating':_num(row['rating']),
                            'rareflag':int(_num(row['rating'])>=75),
                            'nationId':_num(row.get('nation')),'teamid':_num(row.get('teamid')),
                            'leagueId':_num(row.get('leagueId')),'preferredPosition':pos,
                            'name':row.get('name',''),'clubName':row.get('teamName',''),
                            'leagueName':row.get('leagueName',''),
                            'attributeArray':[_num(v) for v in row.get('face',[])[:6]],
                            'dataSource':'user-supplied-fifa22-catalogue'})
        by_asset={row['assetId']:row for row in players}
        specials=json.loads(SPECIALS.read_text(encoding='utf-8'))['cards'] if SPECIALS.is_file() else []
        # Card background shell IDs (decimal filenames): 5 = TOTY, 11 = TOTS,
        # per the supplied shell mapping. The manifest served to the client
        # carries entries for both.
        rarity={'TOTY':5,'TOTS':11}
        for card in specials:
            if card.get('version') not in rarity:continue
            base=by_asset.get(card.get('assetId'))
            if base is None:continue
            resource_id=card.get('resourceId')
            if not isinstance(resource_id,int) or resource_id%(1<<24)!=base['assetId']:
                continue
            players.append({**base,'resourceId':resource_id,
                            'rating':card['rating'],'rareflag':rarity[card['version']],
                            'preferredPosition':card['position'],
                            'attributeArray':card['attributeArray'],
                            'special':True,'specialVersion':card['version'],
                            'resourceIdStatus':'futbin-cutout-filename',
                            'dataSource':'futbin-fifa22-listing'})
        # The user's larger FUT list supplies real edition IDs and face stats,
        # but no rarity IDs or image files. Keep those editions searchable in
        # the catalogue. Only the three portrait-backed cards above are
        # granted to the current club automatically.
        known={row['resourceId'] for row in players}
        supplied=json.loads(SUPPLIED_SPECIALS.read_text(encoding='utf-8-sig')) if SUPPLIED_SPECIALS.is_file() else {}
        for key,card in supplied.items():
            if not isinstance(card,dict):continue
            if (_num(card.get('leagueId'))==2136
                    or ' women' in str(card.get('teamName','')).lower()):
                continue
            asset=card.get('assetId');resource_id=card.get('resourceId')
            base=by_asset.get(asset)
            attrs=card.get('attributes')
            if (type(resource_id) is not int or str(resource_id)!=key
                    or card.get('position') not in POSITIONS
                    or not isinstance(attrs,list) or len(attrs)!=6
                    or not all(type(v) is int and 0<=v<=99 for v in attrs)
                    or type(card.get('rating')) is not int or not 1<=card['rating']<=99):
                continue
            promo=card.get('promo','')
            promo_color=card.get('promoColor','')
            rareflag=PROMO_RARITY.get(promo_color,PROMO_RARITY.get(promo,1))
            if resource_id==asset and base is not None:
                # ICON-style edition: the promo card IS the base asset, so
                # promote the base row instead of adding a duplicate id.
                base.update({'rareflag':rareflag,'rating':card['rating'],
                             'preferredPosition':card['position'],
                             'attributeArray':attrs,
                             'nationId':_num(card.get('nation')),
                             'teamid':_num(card.get('teamId')),
                             'leagueId':_num(card.get('leagueId')),
                             'special':rareflag>1,
                             'specialVersion':promo_color or promo,
                             'promo':promo,
                             'resourceIdStatus':'user-supplied-specials-file',
                             'dataSource':'user-supplied-fut22-specials'})
                continue
            if resource_id in known or resource_id%(1<<24)!=asset:
                continue
            if base is None:
                # FUT-only card (most ICONs): the career roster has no base
                # row, so build the player entirely from the specials file.
                players.append({'resourceId':resource_id,'assetId':asset,
                                'rating':card['rating'],'rareflag':rareflag,
                                'nationId':_num(card.get('nation')),
                                'teamid':_num(card.get('teamId')),
                                'leagueId':_num(card.get('leagueId')),
                                'preferredPosition':card['position'],
                                'name':card.get('name',''),
                                'clubName':'','leagueName':'',
                                'attributeArray':attrs,
                                'special':rareflag>1,
                                'specialVersion':promo_color or promo,
                                'promo':promo,
                                'resourceIdStatus':'user-supplied-specials-file',
                                'dataSource':'user-supplied-fut22-specials'})
                known.add(resource_id)
                continue
            if resource_id<=asset:
                continue
            players.append({**base,'resourceId':resource_id,
                            'rating':card['rating'],'rareflag':rareflag,
                            'preferredPosition':card['position'],
                            'attributeArray':attrs,
                            'nationId':_num(card.get('nation')),
                            'teamid':_num(card.get('teamId')),
                            'leagueId':_num(card.get('leagueId')),
                            'special':rareflag>1,
                            'specialVersion':promo_color or promo,
                            'promo':promo,
                            'resourceIdStatus':'user-supplied-specials-file',
                            'dataSource':'user-supplied-fut22-specials'})
            known.add(resource_id)
        return {'game':'fifa22','source':SOURCE,'players':players}
    links={int(row['teamid']):int(row['leagueid']) for row in json.loads(LINKS.read_text(encoding='utf8'))}
    with CSV.open(encoding='utf-8-sig',newline='') as source:
        rows=list(csv.DictReader(source))
    by_name=defaultdict(Counter)
    for row in rows:
        if str(row.get('gender','')).strip().lower() not in ('','male','m'):continue
        league=links.get(_num(row['club_team_id']))
        if league:by_name[row['league_name']][league]+=1
    dominant={name:counts.most_common(1)[0][0] for name,counts in by_name.items()
              if counts.most_common(1)[0][1]/sum(counts.values())>=0.7}
    players=[]
    for row in rows:
        if str(row.get('gender','')).strip().lower() not in ('','male','m'):continue
        asset=_num(row['sofifa_id']);rating=_num(row['overall'])
        pos=row['player_positions'].split(',')[0].strip().upper()
        if not asset or not 1<=rating<=99 or pos not in POSITIONS:continue
        team=_num(row['club_team_id']);nation=_num(row['nationality_id'])
        league=dominant.get(row['league_name'],links.get(team,0))
        face=[_num(row[k]) for k in (('goalkeeping_diving','goalkeeping_handling','goalkeeping_kicking','goalkeeping_reflexes','goalkeeping_speed','goalkeeping_positioning') if pos=='GK' else ('pace','shooting','passing','dribbling','defending','physic'))]
        players.append({'resourceId':asset,'assetId':asset,'rating':rating,'rareflag':int(rating>=75),
                        'nationId':nation,'teamid':team,'leagueId':league,'preferredPosition':pos,
                        'name':row['short_name'],'clubName':row['club_name'],'leagueName':row['league_name'],
                        'attributeArray':face,'dataSource':'fifa22-career-roster'})
    return {'game':'fifa22','source':FALLBACK_SOURCE,'players':players}

def starter_ids(players):
    """Pick a balanced, deterministic Premier League base squad."""
    by_pos=defaultdict(list)
    for row in players:
        if row['leagueId']==13 and row['teamid'] and 60<=row['rating']<=74:
            by_pos[row['preferredPosition']].append(row)
    for group in by_pos.values():group.sort(key=lambda r:(abs(r['rating']-67),r['resourceId']))
    order=('GK','RB','CB','CB','LB','RM','CM','CM','LM','ST','ST',
           'GK','RB','CB','CB','LB','RM','CM','CM','LM','ST','ST','ST')
    selected=[]
    for position in order:
        choice=next((r for r in by_pos[position] if r['resourceId'] not in selected),None)
        if choice is None:raise ValueError(f'Not enough FIFA 22 starter players at {position}')
        selected.append(choice['resourceId'])
    return selected

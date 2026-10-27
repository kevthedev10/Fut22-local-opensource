"""Audited Basic, Advanced and repeatable Upgrade SBC catalogue.

National Pride is deliberately omitted. Rewards use this server's real Store
pack definitions, so completing a challenge creates an unopened My Packs entry
and repeatable upgrades grant one reward pack per completed cycle.
"""

def pack(pack_id,count=1):
    return {'type':'PACK','packId':int(pack_id),'count':int(count)}

def coins(value):
    return {'type':'COINS','value':int(value),'count':1}

def rule(kind,value,scope='min',count=-1,text=''):
    return {'kind':kind,'value':int(value),'scope':scope,
            'count':int(count),'text':text}

def challenge(challenge_id,name,players,rules,awards):
    return {'challengeId':int(challenge_id),'name':name,'description':name,
            'players':int(players),'formation':'f442',
            'rules':[rule('player_count',players,'exact',text=f'Players: Exactly {players}'),
                     *rules],
            'awards':awards}

def sbc_set(set_id,category_id,priority,name,awards,challenges,repeatable=False):
    return {'setId':int(set_id),'categoryId':int(category_id),
            'category':{1:'BASIC',2:'ADVANCED',3:'UPGRADES'}.get(category_id,'OTHER'),
            'priority':int(priority),'name':name,'description':name,
            'repeatable':bool(repeatable),'awards':awards,'challenges':challenges}

SBC_CATALOG=[
 sbc_set(1,1,1,"Let's Get Started",[pack(1021)],[
  challenge(101,'First Exchange',1,[rule('quality',1,'exact',text='Player Quality: Bronze')],[pack(1001)]),
  challenge(102,'The Second Step',3,[rule('rare_count',0,'exact',text='Common Players: Exactly 3'),rule('chemistry',12,text='Chemistry: Min. 12')],[pack(1001)]),
  challenge(103,'The Third Step',4,[rule('nation_count',3,'exact',text='Nationalities: Exactly 3'),rule('chemistry',17,text='Chemistry: Min. 17')],[pack(1002)]),
 ]),
 sbc_set(2,1,2,'League and Nation Basics',[pack(1022)],[
  challenge(201,'One League Attack',7,[rule('league_count',1,'exact',text='Leagues: Exactly 1'),rule('nation_count',5,'exact',text='Nationalities: Exactly 5'),rule('chemistry',45,text='Chemistry: Min. 45')],[pack(1012)]),
  challenge(202,'One Nation Midfield',5,[rule('quality',2,'min',3,'Silver Players: Min. 3'),rule('league_count',5,'exact',text='Leagues: Exactly 5'),rule('nation_count',1,'exact',text='Nationalities: Exactly 1'),rule('chemistry',45,text='Chemistry: Min. 45')],[pack(1021)]),
  challenge(203,'Multi League & Nation',6,[rule('club_count',6,'exact',text='Clubs: Exactly 6'),rule('same_league',2,'max',text='Same League: Max. 2'),rule('same_nation',2,'max',text='Same Nation: Max. 2'),rule('team_rating',55,text='Rating: Min. 55'),rule('chemistry',45,text='Chemistry: Min. 45')],[pack(1021)]),
 ]),
 sbc_set(10,1,3,"Let's Keep Going",[pack(1025)],[
  challenge(1001,'Brick Slots',2,[rule('league_count',2,'exact',text='Leagues: Exactly 2'),rule('nation_count',2,'exact',text='Nationalities: Exactly 2'),rule('chemistry',43,text='Chemistry: Min. 43')],[pack(1001)]),
  challenge(1002,'The Correct Position',3,[rule('league_count',3,'exact',text='Leagues: Exactly 3'),rule('nation_count',3,'exact',text='Nationalities: Exactly 3'),rule('chemistry',12,text='Chemistry: Min. 12')],[pack(1011)]),
  challenge(1003,'Perfect Link',5,[rule('quality',2,'min',2,'Silver Players: Min. 2'),rule('same_club',2,'max',text='Same Club: Max. 2'),rule('same_nation',2,'max',text='Same Nation: Max. 2'),rule('chemistry',40,text='Chemistry: Min. 40')],[coins(500),pack(1002)]),
  challenge(1004,'Loyal Lads',3,[rule('league_count',3,'exact',text='Leagues: Exactly 3'),rule('nation_count',1,'exact',text='Nationalities: Exactly 1'),rule('chemistry',30,text='Chemistry: Min. 30')],[pack(1002,2)]),
 ]),
 sbc_set(11,1,4,"Let's Move Forward",[pack(1022)],[
  challenge(1101,'Defensive Development',2,[rule('quality',1,'exact',text='Player Quality: Bronze'),rule('chemistry',8,text='Chemistry: Min. 8')],[pack(1021)]),
  challenge(1102,'A Better Buildup',3,[rule('quality',1,'exact',text='Player Quality: Bronze'),rule('chemistry',8,text='Chemistry: Min. 8')],[pack(1021)]),
  challenge(1103,'Advancing Attack',3,[rule('quality',1,'exact',text='Player Quality: Bronze'),rule('chemistry',9,text='Chemistry: Min. 9')],[pack(1021)]),
 ]),
 sbc_set(20,2,1,'Hybrid Leagues',[pack(1028)],[
  challenge(2001,'Rare Fives',11,[rule('rare_count',5,text='Rare Players: Min. 5'),rule('league_count',5,'exact',text='Leagues: Exactly 5'),rule('same_league',3,'max',text='Same League: Max. 3'),rule('team_rating',68,text='Rating: Min. 68'),rule('chemistry',80,text='Chemistry: Min. 80')],[pack(1025)]),
  challenge(2002,'Seven Suspects',11,[rule('league_count',7,'exact',text='Leagues: Exactly 7'),rule('same_league',3,'max',text='Same League: Max. 3'),rule('team_rating',78,text='Rating: Min. 78'),rule('chemistry',85,text='Chemistry: Min. 85')],[pack(1026)]),
  challenge(2003,'Prime Nine',11,[rule('rare_count',5,text='Rare Players: Min. 5'),rule('league_count',9,'exact',text='Leagues: Exactly 9'),rule('same_league',2,'max',text='Same League: Max. 2'),rule('team_rating',79,text='Rating: Min. 79'),rule('chemistry',99,text='Chemistry: Min. 99')],[pack(1027)]),
  challenge(2004,'Squad Goals',11,[rule('rare_count',6,text='Rare Players: Min. 6'),rule('quality',3,'exact',text='Player Quality: Gold'),rule('league_count',11,'exact',text='Leagues: Exactly 11'),rule('chemistry',100,'exact',text='Chemistry: Exactly 100')],[pack(1027)]),
 ]),
 sbc_set(21,2,2,'Hybrid Nations',[pack(1028)],[
  challenge(2101,'Quads',11,[rule('rare_count',4,text='Rare Players: Min. 4'),rule('nation_count',4,'exact',text='Nationalities: Exactly 4'),rule('same_nation',3,'max',text='Same Nation: Max. 3'),rule('team_rating',75,text='Rating: Min. 75'),rule('chemistry',80,text='Chemistry: Min. 80')],[pack(1025)]),
  challenge(2102,'The Six',11,[rule('nation_count',6,'exact',text='Nationalities: Exactly 6'),rule('same_nation',2,'max',text='Same Nation: Max. 2'),rule('team_rating',70,text='Rating: Min. 70'),rule('chemistry',85,text='Chemistry: Min. 85')],[pack(1022)]),
  challenge(2103,'It Takes Eight',11,[rule('quality',3,'exact',text='Player Quality: Gold'),rule('rare_count',5,text='Rare Players: Min. 5'),rule('nation_count',8,'exact',text='Nationalities: Exactly 8'),rule('same_nation',2,'max',text='Same Nation: Max. 2'),rule('chemistry',88,text='Chemistry: Min. 88')],[pack(1027)]),
  # National Pride intentionally excluded.
 ]),
 sbc_set(22,2,3,'League and Nation Hybrid',[coins(12000)],[
  challenge(2201,'The Puzzler',11,[rule('league_count',2,'exact',text='Leagues: Exactly 2'),rule('same_league',6,'max',text='Same League: Max. 6'),rule('nation_count',3,'exact',text='Nationalities: Exactly 3'),rule('same_nation',4,'max',text='Same Nation: Max. 4'),rule('team_rating',77,text='Rating: Min. 77'),rule('chemistry',100,text='Chemistry: Min. 100')],[pack(1025)]),
  challenge(2202,'Intermediate',11,[rule('league_count',3,'exact',text='Leagues: Exactly 3'),rule('same_league',4,'max',text='Same League: Max. 4'),rule('nation_count',5,'exact',text='Nationalities: Exactly 5'),rule('same_nation',3,'max',text='Same Nation: Max. 3'),rule('team_rating',78,text='Rating: Min. 78'),rule('chemistry',100,text='Chemistry: Min. 100')],[coins(1500),pack(1026)]),
  challenge(2203,'Tough',11,[rule('league_count',6,'exact',text='Leagues: Exactly 6'),rule('same_league',2,'max',text='Same League: Max. 2'),rule('nation_count',6,'exact',text='Nationalities: Exactly 6'),rule('same_nation',2,'max',text='Same Nation: Max. 2'),rule('team_rating',80,text='Rating: Min. 80'),rule('chemistry',89,text='Chemistry: Min. 89')],[pack(1027)]),
  challenge(2204,'Hybrid Master',11,[rule('league_count',7,'exact',text='Leagues: Exactly 7'),rule('nation_count',9,'exact',text='Nationalities: Exactly 9'),rule('team_rating',82,text='Rating: Min. 82'),rule('chemistry',86,text='Chemistry: Min. 86')],[pack(1028)]),
 ]),
 sbc_set(30,3,1,'Bronze Upgrade',[pack(1071)],[
  challenge(3001,'Bronze Upgrade',11,[
   rule('quality',1,'exact',text='Player Quality: Exactly Bronze')],[]),
 ],repeatable=True),
 sbc_set(31,3,2,'Silver Upgrade',[pack(1072)],[
  challenge(3101,'Silver Upgrade',11,[
   rule('quality',2,'exact',text='Player Quality: Exactly Silver')],[]),
 ],repeatable=True),
 sbc_set(32,3,3,'Gold Upgrade',[pack(1073)],[
  challenge(3201,'Gold Upgrade',11,[
   rule('quality',3,'exact',text='Player Quality: Exactly Gold')],[]),
 ],repeatable=True),
]

SET_BY_ID={row['setId']:row for row in SBC_CATALOG}
CHALLENGE_BY_ID={challenge['challengeId']:(row,challenge)
                 for row in SBC_CATALOG for challenge in row['challenges']}

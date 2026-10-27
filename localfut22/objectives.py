"""FIFA 22 Season Objectives catalogue and SCMP response builders.

The local service can prove completed matches and wins, so every objective is
based on those settled results.  Mode-specific metrics deliberately remain
separate: Draft never advances Squad Battles, Rivals or ordinary FUT goals.
"""

CAMPAIGN_ID=2022001
SEASON_SECONDS=365*24*60*60

# Exact FIFA 22 FUT Season 1 XP thresholds.  The final reward unlocks at 100k.
LEVEL_THRESHOLDS=(
    400,1200,2300,3400,4700,6000,7300,8600,10600,12600,
    15100,17600,20600,23600,26600,29600,32600,35600,39600,43600,
    48100,52600,57100,61600,66100,71100,76100,83000,91000,100000,
)

# Every displayed reward resolves to an openable player pack. This avoids the
# regressed Club Items Pack substitutions while keeping a graduated FIFA 22
# season path and finishing with the four promo tiers already in the store.
LEVEL_REWARD_NAMES=(
    'Premium Gold Pack','Premium Gold Pack','Premium Gold Pack','Gold Players Pack',
    'Premium Gold Pack','Gold Players Pack','Jumbo Premium Gold Pack','Gold Players Pack',
    'Gold Players Pack','Gold Players Pack','Gold Players Pack','Gold Players Pack',
    'Premium Gold Pack','Prime Gold Players Pack','Premium Gold Pack','Gold Players Pack',
    'Rare Players Pack','Rare Players Pack','Gold Players Pack','Premium Gold Players Pack',
    'Gold Players Pack','Gold Players Pack','Prime Gold Players Pack','Rare Players Pack',
    'Gold Players Pack','Jumbo Rare Players Pack','Headliners Players Pack',
    'FUT Birthday Players Pack','Team of the Season Players Pack','Ultimate Icons Pack',
)

LEVEL_REWARDS=(
    ('pack',1022,1),('pack',1022,1),('pack',1022,1),('pack',1024,1),
    ('pack',1022,1),('pack',1024,1),('pack',1023,1),('pack',1024,1),
    ('pack',1024,1),('pack',1024,1),('pack',1024,1),('pack',1024,1),
    ('pack',1022,1),('pack',1026,1),('pack',1022,1),('pack',1024,1),
    ('pack',1027,1),('pack',1027,1),('pack',1024,1),('pack',1025,1),
    ('pack',1024,1),('pack',1024,1),('pack',1026,1),('pack',1027,1),
    ('pack',1024,1),('pack',1028,1),('pack',1041,1),('pack',1042,1),
    ('pack',1043,1),('pack',1044,1),
)

# (objective id, title, description, metric, target, XP reward)
CATEGORIES=(
    {
        'categoryId':1,'name':'Season Objectives','type':0,'priority':0,
        'groups':(
            {
                'groupId':2201,'title':'Welcome to FUT 22',
                'description':'Play Ultimate Team matches and earn Season XP.',
                'reward':('pack',1022,1),
                'objectives':(
                    (2211,'FUT Debut','Play 1 FUT match','fut.matches',1,500),
                    (2212,'Winning Start','Win 1 FUT match','fut.wins',1,750),
                    (2213,'FUT Regular','Play 5 FUT matches','fut.matches',5,1250),
                ),
            },
            {
                'groupId':2202,'title':'Squad Battles',
                'description':'Take on featured offline squads in Squad Battles.',
                'reward':('pack',1026,1),
                'objectives':(
                    (2221,'Battle Debut','Play 1 Squad Battles match','squad_battles.matches',1,500),
                    (2222,'Battle Winner','Win 1 Squad Battles match','squad_battles.wins',1,750),
                    (2223,'Squad Battles Run','Play 5 Squad Battles matches','squad_battles.matches',5,1250),
                ),
            },
            {
                'groupId':2203,'title':'Single Player Draft',
                'description':'Build a Draft squad and progress through the knockout rounds.',
                'reward':('pack',1027,1),
                'objectives':(
                    (2231,'Draft Debut','Play 1 Single Player Draft match','draft.matches',1,500),
                    (2232,'Draft Victory','Win 1 Single Player Draft match','draft.wins',1,750),
                    (2233,'Draft Run','Play 4 Single Player Draft matches','draft.matches',4,1500),
                ),
            },
            {
                'groupId':2204,'title':'Division Rivals',
                'description':'Compete against other clubs in Division Rivals.',
                'reward':('pack',1028,1),
                'objectives':(
                    (2241,'Rivals Debut','Play 1 Division Rivals match','rivals.matches',1,500),
                    (2242,'Rivals Winner','Win 1 Division Rivals match','rivals.wins',1,750),
                    (2243,'Rivals Run','Play 5 Division Rivals matches','rivals.matches',5,1500),
                ),
            },
            {
                'groupId':2205,'title':'Pack Explorer',
                'description':'Open packs and grow your Ultimate Team club.',
                'reward':('pack',1026,1),
                'objectives':(
                    (2251,'First Reveal','Open 1 pack','packs.opened',1,500),
                    (2252,'Pack Session','Open 5 packs','packs.opened',5,1000),
                    (2253,'Club Builder','Pack 50 items','packs.items',50,1500),
                ),
            },
            {
                'groupId':2206,'title':'Promo Collector',
                'description':'Open FIFA 22 promo packs from the Promo store category.',
                'reward':('pack',1028,1),
                'objectives':(
                    (2261,'Promo Debut','Open 1 promo pack','packs.promo',1,750),
                    (2262,'Promo Trio','Open 3 promo packs','packs.promo',3,1250),
                ),
            },
        ),
    },
)


def metric_value(record,metric):
    mode,field=metric.split('.',1)
    values=record.get(mode,{})
    if not isinstance(values,dict):return 0
    return max(0,int(values.get(field,0) or 0))


def reward_wire(reward):
    kind,value,count=reward
    pack_assets={1001:1,1002:1,1003:1,1011:2,1012:2,1013:2}
    hal_id=pack_assets.get(int(value),3) if kind=='pack' else 0
    return {'awardType':kind,'type':kind,'value':int(value),
            'awardValue':int(value),'count':int(count),'awardCount':int(count),
            'halId':hal_id,'isUntradeable':kind=='pack',
            'untradeable':kind=='pack','loan':0}


def objective_definition(objective_id):
    for category in CATEGORIES:
        for group in category['groups']:
            for objective in group['objectives']:
                if objective[0]==int(objective_id):return group,objective
    return None


def group_definition(group_id):
    for category in CATEGORIES:
        for group in category['groups']:
            if group['groupId']==int(group_id):return category,group
    return None


def level_reward(level_id,option_id=0):
    if int(option_id)!=0 or not 1<=int(level_id)<=len(LEVEL_REWARDS):return None
    return LEVEL_REWARDS[int(level_id)-1]


def level_threshold(level_id):
    if not 1<=int(level_id)<=len(LEVEL_THRESHOLDS):return 10**9
    return LEVEL_THRESHOLDS[int(level_id)-1]


def settled_match_xp(mode,result):
    if mode not in ('squad_battles','draft','rivals','champion','generic'):return 0
    base=125 if mode in ('rivals','champion') else 100
    return base+(50 if result=='WIN' else 25 if result=='DRAW' else 0)


def objective_payload(objective,record,claims,end_time):
    objective_id,name,description,metric,target,xp=objective
    progress=min(target,metric_value(record,metric))
    claimed=str(objective_id) in claims.get('objectives',{})
    state='REDEEMED' if claimed else 'COMPLETED' if progress>=target else 'IN_PROGRESS'
    return {'objectiveId':objective_id,'name':name,'description':description,
            'shortDescription':description,'currentProgress':progress,
            'progressCount':target,'state':state,'expiryTime':end_time,
            'awards':[reward_wire(('xp',xp,1))]}


def group_payload(group,record,claims,start_time):
    objectives=[objective_payload(row,record,claims,start_time+SEASON_SECONDS)
                for row in group['objectives']]
    complete=all(row['state']=='REDEEMED' for row in objectives)
    redeemed=str(group['groupId']) in claims.get('groups',{})
    state=2 if redeemed else 1 if complete else 0
    return {'groupId':group['groupId'],'title':group['title'],
            'subTitle':group['description'],'description':group['description'],
            'groupType':0,'groupState':state,'state':state,
            'priority':group['groupId'],'startTime':start_time,
            'endTime':start_time+SEASON_SECONDS,'groupIdsToUnlock':[],
            'objectives':objectives,
            'awardsList':[reward_wire(group['reward'])]}


def campaign_payload(record,claims,xp,start_time,now,revision=0):
    categories=[]
    for category in CATEGORIES:
        categories.append({'categoryId':category['categoryId'],'name':category['name'],
                           'type':category['type'],'priority':category['priority'],
                           'groupsList':[group_payload(group,record,claims,start_time)
                                         for group in category['groups']]})
    levels=[]
    for level_id,reward in enumerate(LEVEL_REWARDS,1):
        claimed=str(level_id) in claims.get('levels',{})
        levels.append({'levelId':level_id,
                       'levelName':LEVEL_REWARD_NAMES[level_id-1],
                       'rewardName':LEVEL_REWARD_NAMES[level_id-1],
                       'xpThreshold':level_threshold(level_id),
                       'chosenOption':0 if claimed else -1,
                       'rewardOptionList':[{'optionId':0,
                                            'awards':[reward_wire(reward)],
                                            'hiddenReward':False}]})
    category_progress=[]
    for category in categories:
        groups=category['groupsList']
        completed=sum(1 for row in groups if int(row['groupState'])>=1)
        category_progress.append({'categoryId':category['categoryId'],
                                  'groupsCompleted':completed,
                                  'groupsCount':len(groups),
                                  'completed':bool(groups) and completed==len(groups)})
    current_xp=max(0,int(xp))
    return {'campaignId':CAMPAIGN_ID,'campaignName':'ReFUT Season 1',
            'campaignScore':current_xp,'title':'ReFUT Season 1',
            'subtitle':'Complete Objectives to earn rewards',
            'startTime':int(start_time),'endTime':int(start_time)+SEASON_SECONDS,
            'serverCrtTime':int(now),'userXp':current_xp,
            'userXpList':[{'campaignId':CAMPAIGN_ID,'xp':current_xp}],
            'scmpCategoryProgressList':category_progress,
            'categoriesList':categories,'categories':categories,
            'levelsList':levels,'levels':levels,'hasPreviousCampaign':False,
            'needsAutoClaim':False,'needsWorldAutoclaim':False,
            'needsGroupsRefresh':bool(revision),'revision':int(revision),
            'objectivesRevision':int(revision),
            'remainingRewards':sum(1 for row in levels
                                   if current_xp>=row['xpThreshold']
                                   and row['chosenOption']<0)}

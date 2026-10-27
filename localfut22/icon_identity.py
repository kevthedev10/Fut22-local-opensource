"""Client-facing icon identity aliases for FIFA 22's local FUT name table.

The supplied archive keeps the original edition IDs in storage. The client
labels some standalone icon asset IDs as Stockdale. An icon's lowest version
is a nameable anchor; other versions can be presented as revisions of it.
This map is reversible so ratings and art still resolve from the real card.
"""
import json
from pathlib import Path

MAP_FILE=Path(__file__).resolve().parents[1]/'data/fut22_icon_rebase.json'

def _load():
    rows=json.loads(MAP_FILE.read_text(encoding='utf-8')).get('map',{})
    forward={}
    reverse={}
    for original,entry in rows.items():
        original=int(original)
        anchor=int(entry['anchor'])
        revision=int(entry['revision'])
        if not 0<anchor<1<<24 or not 1<=revision<=15:
            raise ValueError(f'Invalid icon alias for {original}')
        presented=(revision<<24)|anchor
        if presented in reverse:
            raise ValueError(f'Duplicate icon alias {presented}')
        forward[original]=presented
        reverse[presented]=original
    return forward,reverse

FORWARD,REVERSE=_load()

def presented_id(original):
    return FORWARD.get(int(original),int(original))

def original_id(presented):
    return REVERSE.get(int(presented),int(presented))

def presented_asset(original,asset):
    return presented_id(original)&0xffffff if int(original) in FORWARD else int(asset)

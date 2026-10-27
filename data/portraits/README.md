These three player cutouts were obtained from the visible FIFA 22 player pages on FUTBIN on 18 September 2026 for this local prototype:

- `p100821319.webp`: [Lionel Messi, 98 TOTY](https://www.futbin.com/22/player/22633/lionel-messi).
- `p100851841.png`: [Robert Lewandowski, 98 TOTY](https://www.futbin.com/22/player/22632/robert-lewandowski).
- `p218268961.png`: [Karim Benzema, 98 TOTS](https://www.futbin.com/22/player/25530/karim-benzema).

The numeric filenames are the edition IDs observed in FUTBIN's rendered image URLs. The images are 172 by 201 pixels. `tools/build_player_heads.py` preserves their aspect ratios on transparent 180 by 180 canvases, converts them to DXT5 DDS, and combines them with the supplied reference heads in `data/fut2dheads.big`. Game materials belong to Electronic Arts and its licensors. The local client must still verify the archive's portrait lookup behavior.

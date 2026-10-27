"""Check or replace only the known GOS RSA modulus in one running FIFA 22 process.

Default is read-only. --apply changes 256 bytes in process memory after exact
path, build and memory-byte validation. --restore reverses that change. The game
executable on disk and the system certificate store are never changed.
"""
import argparse
import ctypes as ct
from ctypes import wintypes as wt
import hashlib
import json
import os
from pathlib import Path
import time

SUPPORTED_GAME_SHA256={
    'fddc0b5b4a093c460a5d0e07e117634366e2da0050f19eed3776c4340a56471a', # EA app build
    '48bb9aaab79950d80a1c440a06c7969214078dec0e0cfe4f70a284fb6709cc51', # Steam build
}
ORIGINAL_PIN_SHA256='1e7a2384d32851ec35672f236d73d16551a72d0c06605e2c2f823c516d392aff'
PIN_RVA=0x6dff4a0
PIN_FILE_OFFSET=0x6dfd2a0
PIN_SIZE=256
# CardsDLL hub squad consumer is called with r8b=1, which mints fresh item
# ids and drops ours. That is the 'You must assign a Manager' loop: Arteta
# draws from userMassInfo (r8b=0) then hub parse throws the id away.
# fifa22work FUT22-MITM.ini Patch.0: RVA 0x37F964  41 B0 01 -> 41 B0 00
CARDSDLL_NAME='cardsdll_win64_retail.dll'
CARDSDLL_SHA256='d6711e3a227a5eaac3285971c44f0f583b90b42b5c2749a1c95f2e479f7164c5'
HUB_SQUAD_RVA=0x37F964
HUB_SQUAD_EXPECT=bytes.fromhex('41B001')
HUB_SQUAD_PATCH=bytes.fromhex('41B000')

def inspect(process_id,game,modulus,apply=False,restore=False,cards_wait_seconds=90):
    if os.name!='nt':raise RuntimeError('Windows only')
    game=Path(game).resolve(strict=True)
    if game.name.lower()!='fifa22.exe':raise ValueError('Expected FIFA22.exe')
    with game.open('rb') as stream:
        digest=hashlib.file_digest(stream,'sha256').hexdigest()
        stream.seek(PIN_FILE_OFFSET);original=stream.read(PIN_SIZE)
    if digest not in SUPPORTED_GAME_SHA256:raise ValueError(f'Unsupported executable hash {digest}; inspect this build before adapting the probe')
    if hashlib.sha256(original).hexdigest()!=ORIGINAL_PIN_SHA256:raise ValueError('The original certificate key is not at the verified offset; no write performed')
    cards_file=game.parent/'CardsDLL_Win64_retail.dll'
    with cards_file.open('rb') as stream:
        cards_digest=hashlib.file_digest(stream,'sha256').hexdigest()
    if cards_digest!=CARDSDLL_SHA256:raise ValueError('Unsupported CardsDLL build; no write performed')
    local=Path(modulus).read_bytes()
    if len(local)!=PIN_SIZE or local==original:raise ValueError('Invalid local RSA modulus')
    k=ct.WinDLL('kernel32',use_last_error=True)
    ps=ct.WinDLL('psapi',use_last_error=True)
    k.OpenProcess.argtypes=[wt.DWORD,wt.BOOL,wt.DWORD];k.OpenProcess.restype=wt.HANDLE
    k.CloseHandle.argtypes=[wt.HANDLE];k.CloseHandle.restype=wt.BOOL
    k.QueryFullProcessImageNameW.argtypes=[wt.HANDLE,wt.DWORD,wt.LPWSTR,ct.POINTER(wt.DWORD)];k.QueryFullProcessImageNameW.restype=wt.BOOL
    ps.EnumProcessModules.argtypes=[wt.HANDLE,ct.POINTER(wt.HMODULE),wt.DWORD,ct.POINTER(wt.DWORD)];ps.EnumProcessModules.restype=wt.BOOL
    ps.GetModuleFileNameExW.argtypes=[wt.HANDLE,wt.HMODULE,wt.LPWSTR,wt.DWORD];ps.GetModuleFileNameExW.restype=wt.DWORD
    k.ReadProcessMemory.argtypes=[wt.HANDLE,ct.c_void_p,ct.c_void_p,ct.c_size_t,ct.POINTER(ct.c_size_t)];k.ReadProcessMemory.restype=wt.BOOL
    k.WriteProcessMemory.argtypes=[wt.HANDLE,ct.c_void_p,ct.c_void_p,ct.c_size_t,ct.POINTER(ct.c_size_t)];k.WriteProcessMemory.restype=wt.BOOL
    k.VirtualProtectEx.argtypes=[wt.HANDLE,ct.c_void_p,ct.c_size_t,wt.DWORD,ct.POINTER(wt.DWORD)];k.VirtualProtectEx.restype=wt.BOOL
    mutate=apply or restore
    access=0x0400|0x0010|(0x0020|0x0008 if mutate else 0)
    handle=k.OpenProcess(access,False,process_id)
    if not handle:raise ct.WinError(ct.get_last_error())
    try:
        name=ct.create_unicode_buffer(32768);length=wt.DWORD(len(name))
        if not k.QueryFullProcessImageNameW(handle,0,name,ct.byref(length)):raise ct.WinError(ct.get_last_error())
        if Path(name.value).resolve()!=game:raise ValueError('Process path does not match selected game')
        modules=(wt.HMODULE*1024)();needed=wt.DWORD()
        if not ps.EnumProcessModules(handle,modules,ct.sizeof(modules),ct.byref(needed)):raise ct.WinError(ct.get_last_error())
        if needed.value<ct.sizeof(wt.HMODULE) or not modules[0]:raise ValueError('Game module is not loaded')
        address=int(modules[0])+PIN_RVA
        def read():
            buf=ct.create_string_buffer(PIN_SIZE);got=ct.c_size_t()
            if not k.ReadProcessMemory(handle,address,buf,PIN_SIZE,ct.byref(got)) or got.value!=PIN_SIZE:raise ct.WinError(ct.get_last_error())
            return buf.raw
        current=read()
        if current not in (original,local):raise ValueError('Loaded memory does not match the verified RSA modulus; no write performed')
        result={'pid':process_id,'game':str(game),'sha256':digest,'address':hex(address),'state':'local' if current==local else 'original','bytes':PIN_SIZE,'changed':False}
        if mutate:
            desired=original if restore else local
            if desired!=current:
                previous=wt.DWORD()
                # Preserve execute availability during the brief write because the
                # pin resides in an executable PE section in this exact build.
                if not k.VirtualProtectEx(handle,address,PIN_SIZE,0x40,ct.byref(previous)):raise ct.WinError(ct.get_last_error())
                try:
                    written=ct.c_size_t();buf=ct.create_string_buffer(desired)
                    if not k.WriteProcessMemory(handle,address,buf,PIN_SIZE,ct.byref(written)) or written.value!=PIN_SIZE:raise ct.WinError(ct.get_last_error())
                    if read()!=desired:raise RuntimeError('Memory write verification failed')
                except Exception:
                    rollback=ct.create_string_buffer(current);written=ct.c_size_t()
                    if not k.WriteProcessMemory(handle,address,rollback,PIN_SIZE,ct.byref(written)) or written.value!=PIN_SIZE or read()!=current:
                        raise RuntimeError('Write failed and original bytes could not be restored; close the game')
                    raise
                finally:
                    ignored=wt.DWORD()
                    if not k.VirtualProtectEx(handle,address,PIN_SIZE,previous.value,ct.byref(ignored)):raise RuntimeError('Unable to restore memory protection; close the game')
                result.update(state='original' if restore else 'local',changed=True)
        deadline=time.time()+(cards_wait_seconds if mutate else 0)
        cards=None
        while True:
            modules=(wt.HMODULE*1024)();needed=wt.DWORD()
            if not ps.EnumProcessModules(handle,modules,ct.sizeof(modules),ct.byref(needed)):
                raise ct.WinError(ct.get_last_error())
            count=max(1,needed.value//ct.sizeof(wt.HMODULE))
            for index in range(min(count,len(modules))):
                module=modules[index]
                if not module:continue
                path=ct.create_unicode_buffer(32768)
                if not ps.GetModuleFileNameExW(handle,module,path,len(path)):continue
                if Path(path.value).name.lower()!=CARDSDLL_NAME:continue
                if Path(path.value).resolve()!=cards_file.resolve():raise ValueError('Loaded CardsDLL path does not match the verified file')
                hub=int(module)+HUB_SQUAD_RVA
                buf=ct.create_string_buffer(3);got=ct.c_size_t()
                if not k.ReadProcessMemory(handle,hub,buf,3,ct.byref(got)) or got.value!=3:
                    raise ct.WinError(ct.get_last_error())
                current_hub=buf.raw
                cards={'module':path.value,'address':hex(hub),
                       'state':'patched' if current_hub==HUB_SQUAD_PATCH else
                                'original' if current_hub==HUB_SQUAD_EXPECT else 'unknown',
                       'bytes':current_hub.hex(),'changed':False}
                if current_hub not in (HUB_SQUAD_EXPECT,HUB_SQUAD_PATCH):
                    raise ValueError('CardsDLL hub-squad bytes are not the Aug 10 2022 sequence; no write performed')
                if mutate:
                    desired=HUB_SQUAD_EXPECT if restore else HUB_SQUAD_PATCH
                    if desired!=current_hub:
                        previous=wt.DWORD()
                        if not k.VirtualProtectEx(handle,hub,3,0x40,ct.byref(previous)):
                            raise ct.WinError(ct.get_last_error())
                        try:
                            written=ct.c_size_t();out=ct.create_string_buffer(desired)
                            if not k.WriteProcessMemory(handle,hub,out,3,ct.byref(written)) or written.value!=3:
                                raise ct.WinError(ct.get_last_error())
                            verify=ct.create_string_buffer(3);got=ct.c_size_t()
                            if not k.ReadProcessMemory(handle,hub,verify,3,ct.byref(got)) or verify.raw!=desired:
                                raise RuntimeError('CardsDLL hub-squad write verification failed')
                        finally:
                            ignored=wt.DWORD()
                            k.VirtualProtectEx(handle,hub,3,previous.value,ct.byref(ignored))
                        cards.update(state='original' if restore else 'patched',
                                     bytes=desired.hex(),changed=True)
                break
            if cards is not None or time.time()>=deadline:
                break
            print('Waiting for Ultimate Team (CardsDLL) so the manager id can be kept...')
            time.sleep(2)
        if cards is None:
            raise ValueError('CardsDLL_Win64_retail.dll is not loaded. Open Ultimate Team, leave FIFA 22 running, then run Connect-Game again.')
        result['hubSquadParse']=cards
        return result
    finally:k.CloseHandle(handle)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pid',type=int,required=True)
    parser.add_argument('--game',type=Path,default=Path('D:/EA Games/FIFA 22/FIFA22.exe'))
    parser.add_argument('--modulus',type=Path,default=Path('runtime/certs/modulus.bin'))
    parser.add_argument('--cards-wait-seconds',type=int,default=90,
                        help='How long to wait for Ultimate Team to load CardsDLL')
    actions=parser.add_mutually_exclusive_group()
    actions.add_argument('--apply',action='store_true')
    actions.add_argument('--restore',action='store_true')
    args=parser.parse_args()
    if args.cards_wait_seconds<0:parser.error('--cards-wait-seconds must be nonnegative')
    print(json.dumps(inspect(args.pid,args.game,args.modulus,args.apply,args.restore,
                             args.cards_wait_seconds),indent=2))

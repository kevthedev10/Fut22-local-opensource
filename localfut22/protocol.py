"""Bounded FIRE2 framing and TDF parsing, based on the supplied wire-format references.

The framing is shared by the reference servers. This does not establish that
their command payloads are compatible with FIFA 22.
"""
from dataclasses import dataclass
import struct

MAX_PAYLOAD = 8 * 1024 * 1024
MAX_METADATA = 65535

class ProtocolError(ValueError):
    pass

@dataclass(frozen=True)
class Packet:
    component: int
    command: int
    message: int
    kind: int
    payload: bytes = b''
    metadata: bytes = b''
    header_tail: bytes = b'\0\0\0'

    def encode(self):
        if not 0 <= self.component <= 65535 or not 0 <= self.command <= 65535:
            raise ProtocolError('Component/command out of range')
        if not 0 <= self.message <= 0xffffff or not 0 <= self.kind <= 7:
            raise ProtocolError('Message/type out of range')
        if len(self.payload) > MAX_PAYLOAD or len(self.metadata) > MAX_METADATA:
            raise ProtocolError('Frame too large')
        if len(self.header_tail) != 3:
            raise ProtocolError('FIRE2 header tail must be three bytes')
        return (struct.pack('>IHHH',len(self.payload),len(self.metadata),self.component,self.command)
                + self.message.to_bytes(3,'big')
                + bytes([(self.kind << 5) | (self.header_tail[0] & 31)])
                + self.header_tail[1:]
                + self.metadata + self.payload)

def take_packet(buffer):
    if len(buffer) < 16:
        return None
    size,meta,c,k = struct.unpack_from('>IHHH',buffer)
    if size > MAX_PAYLOAD or meta > MAX_METADATA:
        raise ProtocolError('Frame exceeds configured limit')
    end = 16 + meta + size
    if len(buffer) < end:
        return None
    result = Packet(c,k,int.from_bytes(buffer[10:13],'big'),buffer[13] >> 5,
                    bytes(buffer[16+meta:end]),bytes(buffer[16:16+meta]),
                    bytes([buffer[13] & 31])+bytes(buffer[14:16]))
    del buffer[:end]
    return result

def varint(value):
    if not isinstance(value,int) or abs(value) >= 1 << 64:
        raise ProtocolError('TDF integer out of range')
    negative = value < 0
    value = abs(value)
    result = bytearray([(value & 63) | (64 if negative else 0)])
    value >>= 6
    while value:
        result[-1] |= 128
        result.append(value & 127)
        value >>= 7
    return bytes(result)

def tag(name,kind):
    if not 1 <= len(name) <= 4 or any(not ('A' <= c <= 'Z' or '0' <= c <= '9') for c in name):
        raise ProtocolError('TDF tags require one to four uppercase letters/digits')
    packed = 0
    for i,char in enumerate(name):
        packed |= ((ord(char)-32) & 63) << (18 - i*6)
    return packed.to_bytes(3,'big') + bytes([kind])

def string(value):
    raw=value.encode('utf-8')+b'\0'
    return varint(len(raw))+raw

def integer(name,value): return tag(name,0)+varint(value)
def text(name,value): return tag(name,1)+string(str(value))
def blob(name,value): return tag(name,2)+varint(len(value))+value
def group(name,fields): return tag(name,3)+fields+b'\0'
def integer_list(name,values): return tag(name,4)+b'\0'+varint(len(values))+b''.join(varint(v) for v in values)
def string_map(name,values): return tag(name,5)+b'\x01\x01'+varint(len(values))+b''.join(string(k)+string(v) for k,v in values.items())
def empty_map(name,key_type,value_type):
    return tag(name,5)+bytes([key_type,value_type])+varint(0)
def group_map(name,values): return tag(name,5)+b'\x01\x03'+varint(len(values))+b''.join(string(k)+v+b'\0' for k,v in values.items())
def object_id(name,component,entity_type,entity_id):
    return tag(name,9)+varint(component)+varint(entity_type)+varint(entity_id)
def group_list(name,values):
    return tag(name,4)+b'\x03'+varint(len(values))+b''.join(value+b'\0' for value in values)
def string_list(name,values):
    return tag(name,4)+b'\x01'+varint(len(values))+b''.join(string(value) for value in values)

class Reader:
    def __init__(self,data):
        self.data=data
        self.pos=0
        self.nodes=0

    def read(self,count):
        if count < 0 or self.pos+count > len(self.data):
            raise ProtocolError('Truncated TDF value')
        value=self.data[self.pos:self.pos+count]
        self.pos+=count
        return value

    def integer(self):
        first=self.read(1)[0]
        value=first & 63
        shift=6
        byte=first
        while byte & 128:
            if shift>=64: raise ProtocolError('Oversized TDF integer')
            byte=self.read(1)[0]
            value|=(byte & 127)<<shift
            shift+=7
        if value >= 1 << 64: raise ProtocolError('Oversized TDF integer')
        return -value if first & 64 else value

    def count(self):
        count=self.integer()
        if not 0 <= count <= 100000: raise ProtocolError('Invalid TDF collection size')
        return count

    def fields(self,depth=0,terminated=False):
        if depth>24: raise ProtocolError('TDF nesting limit reached')
        fields=[]
        while self.pos<len(self.data):
            if self.data[self.pos]==0:
                if not terminated: raise ProtocolError('Unexpected group terminator')
                self.pos+=1
                return fields
            raw=self.read(4)
            packed=int.from_bytes(raw[:3],'big')
            name=''.join(chr(((packed >> shift)&63)+32) for shift in (18,12,6,0)).rstrip(' ')
            fields.append({'tag':name,'type':raw[3],'value':self.value(raw[3],depth+1)})
        if terminated: raise ProtocolError('Missing group terminator')
        return fields

    def value(self,kind,depth):
        self.nodes+=1
        if depth>24 or self.nodes>100000: raise ProtocolError('TDF complexity limit reached')
        if kind==0: return self.integer()
        if kind==1:
            raw=self.read(self.count())
            if not raw.endswith(b'\0'): raise ProtocolError('Unterminated TDF string')
            return raw[:-1].decode('utf-8',errors='replace')
        if kind==2: return {'hex':self.read(self.count()).hex()}
        if kind==3: return self.fields(depth,terminated=True)
        if kind==4:
            item_type=self.read(1)[0]
            return [self.value(item_type,depth+1) for _ in range(self.count())]
        if kind==5:
            kt,vt=self.read(2)
            return [{'key':self.value(kt,depth+1),'value':self.value(vt,depth+1)} for _ in range(self.count())]
        if kind==6:
            active=self.read(1)[0]
            if active==127:return {'active':active}
            raw=self.read(4)
            packed=int.from_bytes(raw[:3],'big')
            name=''.join(chr(((packed>>s)&63)+32) for s in (18,12,6,0)).rstrip(' ')
            return {'active':active,'tag':name,'type':raw[3],'value':self.value(raw[3],depth+1)}
        if kind==8:return [self.integer(),self.integer()]
        if kind==9:return [self.integer(),self.integer(),self.integer()]
        if kind==10:return struct.unpack('>f',self.read(4))[0]
        raise ProtocolError(f'Unsupported TDF type {kind}; retain raw capture')

def decode(data):
    return Reader(data).fields()

def find(fields,name,default=None):
    for field in fields:
        if field['tag']==name:return field['value']
    return default

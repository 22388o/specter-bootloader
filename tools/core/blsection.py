"""Bootloader sections and operations on them."""

from abc import ABC, abstractmethod
from collections import namedtuple
import pytest
from ctypes import *
import zlib
import sys
from .signature import *
from .signature import _sha256

# Types representing sequence of bytes, for type checking
_byteslike = (bytes, bytearray)
# Types representing sequence of objects, for type checking
_arraylike = (frozenset, list, set, tuple,)

# Section magic word
BL_SECT_MAGIC = 0x54434553 # "SECT" in LE
# Structure revision
STRUCT_REV = 1
# Supported revisions of the structure for deserialization
_supported_revisions = [1]
# Maximum allowed size of payload (16 megabytes)
_max_payload_size = 16 * 1024 * 1024
# Supported digital signature algorithms
_supported_algorithms = [DSA_SECP256K1_SHA256]

# Minimum allowed value ov version number
VERSION_MIN = 1
# Maximum allowed value ov version number
VERSION_MAX = 4199999999
# Version is not available
VERSION_NA = 0
# Version tag embedded somewhere inside payload firmware
VERSION_TAG = b'<version:tag10>'
# Closing version tag
VERSION_TAG_CLOSE = b'</version:tag10>'
# Number of decimal digits in ASCII encoding, following the version tag
VERSION_DIGITS = 10

# Mapping between attribute name and its (code, type)
_attributes = { 'bl_attr_algorithm' : (1, str) }
# Reverse lookup by attribute code
_attribute_names = {v[0]: k for k, v in _attributes.items()}

# Registers additional attributes for testing
@pytest.fixture()
def _add_test_attributes():
    global _attributes, _attribute_names
    _attributes = { **_attributes,
                    'a2': (0xa2, None),
                    'a3': (0xa3, int),
                    'a4': (0xa4, str) }
    _attribute_names = {v[0]: k for k, v in _attributes.items() }

def _validate_array(values, class_=None, accept_empty=False):
    if not isinstance(values, _arraylike):
        raise TypeError("Parameter sections should be array-like")
    if not accept_empty and not len(values):
        raise ValueError("Array shouldn't be empty")
    if class_:
        for value in values:
            if not isinstance(value, class_):
                raise TypeError(f"Values should be instances of {class_}")

def is_version_valid(version_num, allow_na=True):
    """Checks if version is valid"""
    if allow_na and version_num == VERSION_NA:
        return True
    elif version_num >= VERSION_MIN and version_num <= VERSION_MAX:
        return True
    return False

def version_to_str(version_num):
    """Converts version number to string"""
    if not is_version_valid(version_num):
        raise ValueError("Version number is out of range")
    if version_num == VERSION_NA:
        return ""
    major  = version_num // (100 * 1000 * 1000)
    minor  = version_num // (100 * 1000) % 1000
    patch  = version_num // 100 % 1000
    rc_rev = version_num % 100
    ver_str = f"{major}.{minor}.{patch}"
    if rc_rev != 99:
        ver_str += f"-rc{rc_rev}"
    return ver_str

# /// Section header
# ///
# /// This structure has fixed size of 256 bytes. All 32-bit words are stored in
# /// little-endian format. CRC is calculated over first 252 bytes of this
# /// structure.
# typedef struct __attribute__((packed)) bl_section_ {
#   uint32_t magic;         ///< Magic word, BL_SECT_MAGIC
#   uint32_t struct_rev;    ///< Revision of structure format
#   char name[16];          ///< Name, zero terminated
#   uint32_t pl_ver;        ///< Payload version, 0 if not available
#   uint32_t pl_size;       ///< Payload size
#   uint32_t pl_crc;        ///< Payload CRC
#   uint8_t attr_list[216]; ///< Attributes, list of: { key, size [, value] }
#   uint32_t struct_crc;    ///< CRC of this structure using LE representation
# } bl_section_t;
class _bl_section_t(LittleEndianStructure):
    _pack_ = 1        # Pack structure
    _CRC_SIZE = 4     # CRC32 size in bytes
    _PL_VER_SIZE = 4  # Size of payload version in bytes
    _NAME_SIZE = 16   # Name size in bytes

    _fields_ = [('magic',      c_uint32),
                ('struct_rev', c_uint32),
                ('name',       c_char * _NAME_SIZE),
                ('pl_ver',     c_uint32),
                ('pl_size',    c_uint32),
                ('pl_crc',     c_uint32),
                ('attr_list',  c_uint8 * 216),
                ('struct_crc', c_uint32)]

    def __init__(self, name = ""):
        super(_bl_section_t, self).__init__(magic = BL_SECT_MAGIC,
                                           struct_rev = STRUCT_REV,
                                           pl_ver = VERSION_NA)
        self.set_name(name)

    def __eq__(self, other):
        if not isinstance(other, _bl_section_t):
            return NotImplemented
        return bytes(self) == bytes(other)

    @staticmethod
    def _encode_int(value):
        return value.to_bytes((value.bit_length() + 7) // 8,
                              byteorder='little')

    def set_attributes(self, attributes):
        attr_list = []
        for key, value in attributes.items():
            key_byte, attr_type = _attributes[key]
            if attr_type is None:
                if value:
                    raise TypeError("Value should be empty or None")
            elif not isinstance(value, attr_type):
                raise TypeError("Incorrect type of value")
            attr_list.append(key_byte)
            if attr_type is None:
                data = []
            elif isinstance(value, int):
                data = list(self._encode_int(value))
            elif isinstance(value, str):
                data = list(value.encode('ascii'))
            else:
                data = list(value)
            if len(data) > 255:
                raise ValueError("Attribute size exceeded")
            attr_list.append(len(data))
            attr_list.extend(data)

        arr_size = sizeof(self.attr_list)
        if len(attr_list) <= arr_size:
            for i in range(arr_size):
                self.attr_list[i] = 0 if i >= len(attr_list) else attr_list[i]
        else:
            raise ValueError("Attributes do not fit in array")

    def get_attributes(self):
        attributes = {}
        attr_list = bytes(self.attr_list)
        while len(attr_list) > 0:
            key_byte, len_byte = attr_list[:2]
            if not key_byte:
                break
            if len_byte > len(attr_list) - 2:
                raise ValueError("Attribute size exceeded")
            value_buf = attr_list[2:2 + len_byte]
            attr_list = attr_list[2 + len_byte:]

            try:
                key = _attribute_names[key_byte]
                _, attr_type = _attributes[key]
            except KeyError:
                continue # Unknown attribute, skip it

            # Handle attribute according to its type
            if attr_type is None or not len_byte:
                attributes[key] = None
            elif attr_type is int:
                attributes[key] = int.from_bytes(value_buf, byteorder='little')
            elif attr_type is str:
                attributes[key] = value_buf.decode('ascii')
            else:
                attributes[key] = attr_type(value_buf)

        return attributes

    def calc_crc(self):
        data = bytes(self)[:sizeof(self) - self._CRC_SIZE]
        self.struct_crc = zlib.crc32(data)

    def check_crc(self):
        data = bytes(self)[:sizeof(self) - self._CRC_SIZE]
        crc = zlib.crc32(data)
        return crc == self.struct_crc

    def set_name(self, name):
        if len(name) > self._NAME_SIZE - 1:
            raise ValueError("Section name is too long")
        if isinstance(name, str):
            self.name = name.encode('ascii')
        elif isinstance(name, _byteslike):
            self.name = name
        else:
            raise ValueError("Name must be str or bytes-like")

    def get_name(self):
        return self.name.decode('ascii')

    def serialize_name(self):
        return self.name + bytes(self._NAME_SIZE - len(self.name))

    def get_pl_ver_str(self):
        return version_to_str(self.pl_ver)

    def serialize_pl_ver(self):
        return self.pl_ver.to_bytes(self._PL_VER_SIZE, byteorder='little')

    def serialize(self):
        return bytes(self)

    # Raises ValueError if header is incorrect
    def validate(self):
        if self.magic != BL_SECT_MAGIC:
            raise ValueError("Not a section header")
        if not self.struct_rev in _supported_revisions:
            raise ValueError("Unsupported revision of section header")
        if not self.check_crc():
            raise ValueError("Incorrect section CRC")
        try:
            if len(self.get_name()) >  self._NAME_SIZE - 1:
                raise ValueError()
        except:
            raise ValueError("Incorrect section name")
        if not is_version_valid(self.pl_ver):
            raise ValueError("Incorrect version of payload")
        if self.pl_size > _max_payload_size:
            raise ValueError("Payload is larger than allowed")
        try:
            _ = self.get_attributes()
        except:
            raise ValueError("Incorrect section attributes")


# Signature record (internal representation)
class _bl_signature_rec_t(LittleEndianStructure):
    _pack_ = 1    # Pack structure
    _fields_ = [('fingerprint', c_uint8 * 16),
                ('signature',   c_uint8 * 64)]

    @property
    def pair(self):
        return (bytes(self.fingerprint), bytes(self.signature))

    @pair.setter
    def pair(self, value):
        fp, sig = value
        self.fingerprint = (c_uint8 * 16)(*fp)
        self.signature   = (c_uint8 * 64)(*sig)


class Section(ABC):
    """Abstract base class for Bootloader sections"""

    def __init__(self, name="", header=None):
        """Constructs a new Section"""
        if header is None:
            self._header = _bl_section_t(name)
        elif not name:
            self._header = header
        else:
            raise ValueError("Section name is specified for existing header")

        super().__init__()

    @property
    def name(self):
        return self._header.get_name()

    @name.setter
    def name(self, value):
        self._header.set_name(value)

    @property
    def attributes(self):
        return self._header.get_attributes()

    @attributes.setter
    def attributes(self, value):
        self._header.set_attributes(value)

    @property
    def version(self):
        return self._header.pl_ver

    @property
    def version_str(self):
        return self._header.get_pl_ver_str()

    @abstractmethod
    def _serialize_payload(self):
        pass

    def serialize(self):
        """Serializes section into bytes"""
        payload = self._serialize_payload()
        self._header.pl_size = len(payload)
        self._header.pl_crc = zlib.crc32(payload)
        self._header.calc_crc()
        return self._header.serialize() + payload

    # Returns (section, new_offset)
    @staticmethod
    def deserialize(source, offset_=0):
        """Deserializes, creating a Section from bytes"""
        # Deserialize and check the header
        offset = offset_
        if not isinstance(source, _byteslike):
            raise TypeError("Buffer should be bytes-like")
        if len(source) - offset < sizeof(_bl_section_t):
            raise ValueError("Buffer is less than section header")
        header = _bl_section_t.from_buffer_copy(source, offset)
        offset += sizeof(header)
        header.validate()

        # Deserialize and check the payload
        if len(source) - offset < header.pl_size:
            raise ValueError("Buffer doesn't have enough bytes for payload")
        payload = source[offset : offset + header.pl_size]
        offset += header.pl_size
        if len(payload) != header.pl_size:
            raise ValueError("Payload has wrong size")
        if zlib.crc32(payload) != header.pl_crc:
            raise ValueError("Incorrect payload CRC")

        # Identify section type by name and crete a new object
        classes = { b'sign' : SignatureSection }
        cls = classes.get(header.name, PayloadSection)
        sect = cls(header=header, payload=payload)
        return (sect, offset)

    def get_hash_sentence(self):
        """Returns hash sentence containing section's name, version and hash"""
        hashcode = _sha256(self.serialize())
        name_bytes = self._header.serialize_name()
        version_bytes = self._header.serialize_pl_ver()
        return name_bytes + version_bytes + hashcode

class PayloadSection(Section):
    """Payload section storing firmware"""

    def __init__(self, name="", payload=None, attributes=None, header=None):
        """Constructs a new PayloadSection"""
        super().__init__(name=name, header=header)
        self.payload = payload
        if attributes is not None:
            self._header.set_attributes(attributes)

    @property
    def payload(self):
        return self.__payload

    @payload.setter
    def payload(self, value):
        if not value:
            self.__payload = b''
            self._header.pl_ver = VERSION_NA
        elif isinstance(value, _byteslike):
            self.__payload = value
            self._header.pl_ver = self._find_payload_version()
        else:
            raise TypeError("Payload must be bytes-like")

    def __eq__(self, other):
        """Return True if self==other (same header and payload)"""
        if not isinstance(other, PayloadSection):
            return False if isinstance(other, Section) else NotImplemented
        return ( self._header == other._header and
                 self.__payload == other.__payload )

    def _find_payload_version(self):
        # Search for version tag in payload
        idx = self.__payload.find(VERSION_TAG)
        if idx < 0:
            return VERSION_NA

        # Ensure that there is no more version tags
        idx2 = self.__payload.find(VERSION_TAG, idx + 1)
        if idx2 >= 0:
            raise ValueError("Payload contains more than one version tag")

        # Skip version tag and decode digits
        idx += len(VERSION_TAG)
        if len(self.__payload) < idx + VERSION_DIGITS + len(VERSION_TAG_CLOSE):
            raise ValueError("Corrupted varsion tag in payload")
        version_num = int(self.__payload[idx : (idx + VERSION_DIGITS)])
        if not is_version_valid(version_num, allow_na=False):
            raise ValueError("Version number is out of range")

        # Check that closing tag is present
        idx += VERSION_DIGITS
        closing_tag = self.__payload[idx : (idx + len(VERSION_TAG_CLOSE))]
        if closing_tag != VERSION_TAG_CLOSE:
            raise ValueError("Corrupted varsion tag in payload")

        # Return version number
        return version_num

    def _serialize_payload(self):
        return self.__payload


class SignatureSection(Section):
    """Signature section storing signature records"""

    def __init__(self, dsa_algorithm='secp256k1-sha256', header=None,
                 payload=None):
        """Constructs a new SignatureSection"""
        name = None if header else 'sign'
        super().__init__(name=name, header=header)
        self.__signatures = { } # Public dict { fingerprint : signature }
        if header is None:
            self._init_new(dsa_algorithm)
        else:
            self._init_from_header(payload)

    def _init_new(self, dsa_algorithm):
        if not dsa_algorithm in _supported_algorithms:
            raise ValueError("Digital signature algorithm not supported")
        self._header.set_attributes({ 'bl_attr_algorithm' : dsa_algorithm })

    def _init_from_header(self, payload):
        # Check payload
        if not isinstance(payload, _byteslike):
            raise TypeError("Payload must be bytes-like")
        if len(payload) % sizeof(_bl_signature_rec_t) != 0:
            raise TypeError("Payload size must be multiple of record size")

        # Deserialize records
        offset = 0
        while offset < len(payload):
            rec = _bl_signature_rec_t.from_buffer_copy(payload, offset)
            self.__signatures[bytes(rec.fingerprint)] = bytes(rec.signature)
            offset += sizeof(_bl_signature_rec_t)

    @property
    def signatures(self):
        return self.__signatures

    @signatures.setter
    def signatures(self, value):
        self._validate_signatures(value)
        self.__signatures = value

    def __eq__(self, other):
        if not isinstance(other, SignatureSection):
            return False if isinstance(other, Section) else NotImplemented
        return ( self._header == other._header and
                 self.__signatures == other.__signatures )

    @staticmethod
    def _validate_fingerprint(fingerprint):
        if not isinstance(fingerprint, _byteslike):
            raise TypeError("Fingerprint should be bytes-like")
        if len(fingerprint) != FINGERPRINT_LEN:
            raise ValueError(
                f"Fingerprint should be {FINGERPRINT_LEN} bytes long")

    @staticmethod
    def _validate_signature(signature):
        if not isinstance(signature, _byteslike):
            raise TypeError("Signature should be bytes-like")
        if len(signature) != SIGNATURE_LEN:
            raise ValueError(
                f"Fingerprint should be {SIGNATURE_LEN} bytes long")

    @classmethod
    def _validate_signatures(cls, signatures):
        if not isinstance(signatures, dict):
            raise ValueError("Signatures must be dict")
        for fp, sig in signatures.items():
            cls._validate_fingerprint(fp)
            cls._validate_signature(sig)

    def _serialize_payload(self):
        payload_bytes = b''
        self._validate_signatures(self.__signatures)
        for fp, sig in self.__signatures.items():
            #rec = _bl_signature_rec_t(fingerprint=fp, signature=sig)
            rec = _bl_signature_rec_t()
            rec.pair = (fp, sig)
            payload_bytes += bytes(rec)
        return payload_bytes

    def get_hash_sentence(self):
        """"Rises NotImplementedError (signature sections should not be signed
        themselves)."""
        raise NotImplementedError()

def make_signature_message(sections):
    """Creates a bytes message with names, versions and hashes of all payload
    sections. Used as input to signature algorithm.
    """
    _validate_array(sections, class_=PayloadSection)
    message = b''
    for sect in sections:
        message += sect.get_hash_sentence()
    return message
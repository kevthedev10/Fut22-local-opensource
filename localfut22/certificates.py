"""Create a local-only test certificate for the reference GOS trust mechanism."""
import argparse
from datetime import datetime,timedelta,timezone
from pathlib import Path
from cryptography import x509
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID,ExtendedKeyUsageOID

def generate(directory):
    directory=Path(directory)
    directory.mkdir(parents=True,exist_ok=True)
    names=('ca.pem','redirector.pem','redirector-key.pem','modulus.bin')
    existing=[(directory/name).exists() for name in names]
    if all(existing):return directory
    if any(existing):raise ValueError('Partial certificate set exists; choose a new directory')
    now=datetime.now(timezone.utc)
    # Matching the embedded issuer DN is part of the reference trust bridge.
    ca_name=x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME,'US'),
        x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME,'California'),
        x509.NameAttribute(NameOID.LOCALITY_NAME,'Redwood City'),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME,'Electronic Arts, Inc.'),
        x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME,'Global Online Studio'),
        x509.NameAttribute(NameOID.EMAIL_ADDRESS,'GOSDirtysockSupport@ea.com'),
        x509.NameAttribute(NameOID.COMMON_NAME,'GOS 2015 Certificate Authority'),
    ])
    ca_key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    ca=(x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(days=1)).not_valid_after(now+timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True,path_length=0),critical=True)
        .add_extension(x509.KeyUsage(True,False,False,False,False,True,True,None,None),critical=True).sign(ca_key,hashes.SHA256()))
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    leaf_name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'spring18.gosredirector.ea.com')])
    cert=(x509.CertificateBuilder().subject_name(leaf_name).issuer_name(ca_name).public_key(key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(days=1)).not_valid_after(now+timedelta(days=90))
          .add_extension(x509.BasicConstraints(ca=False,path_length=None),critical=True)
          .add_extension(x509.SubjectAlternativeName([x509.DNSName('spring18.gosredirector.ea.com')]),critical=False)
          .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),critical=False)
          .add_extension(x509.KeyUsage(True,False,True,False,False,False,False,None,None),critical=True).sign(ca_key,hashes.SHA256()))
    (directory/'ca.pem').write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    # As in Project21, serve the leaf alone; the game provides the trust anchor.
    (directory/'redirector.pem').write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (directory/'redirector-key.pem').write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    (directory/'modulus.bin').write_bytes(ca_key.public_key().public_numbers().n.to_bytes(256,'big'))
    return directory

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',type=Path,default=Path('runtime/certs'))
    args=parser.parse_args()
    print(generate(args.directory).resolve())

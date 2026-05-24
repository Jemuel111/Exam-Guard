from py_vapid import Vapid
import base64

def generate_vapid_keys():
    print("Generating VAPID keys...\n")

    v = Vapid()
    v.generate_keys()

    # Get the raw public key bytes and encode as URL-safe base64 (no padding)
    # This is the format browsers expect for applicationServerKey
    pub_key = v.public_key
    pri_key = v.private_key

    # Export public key as uncompressed EC point (65 bytes) → URL-safe base64
    public_key_bytes  = pub_key.public_bytes(
        encoding=__import__('cryptography.hazmat.primitives.serialization', fromlist=['Encoding']).Encoding.X962,
        format=__import__('cryptography.hazmat.primitives.serialization', fromlist=['PublicFormat']).PublicFormat.UncompressedPoint
    )
    public_key_b64 = base64.urlsafe_b64encode(public_key_bytes).rstrip(b'=').decode('utf-8')

    # Export private key as raw bytes → URL-safe base64
    private_key_bytes = pri_key.private_bytes(
        encoding=__import__('cryptography.hazmat.primitives.serialization', fromlist=['Encoding']).Encoding.PEM,
        format=__import__('cryptography.hazmat.primitives.serialization', fromlist=['PrivateFormat']).PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=__import__('cryptography.hazmat.primitives.serialization', fromlist=['NoEncryption']).NoEncryption()
    )
    # For pywebpush, the private key can be passed as PEM string
    private_key_str = private_key_bytes.decode('utf-8').strip()

    print("=" * 60)
    print("Add these to your .env file (each on ONE line):")
    print("=" * 60)
    print()
    print(f"VAPID_PUBLIC_KEY={public_key_b64}")
    print()
    # For .env, encode private key as base64 to keep it one line
    private_key_b64 = base64.urlsafe_b64encode(private_key_bytes).rstrip(b'=').decode('utf-8')
    print(f"VAPID_PRIVATE_KEY={private_key_b64}")
    print()
    print("VAPID_CLAIMS_EMAIL=mailto:jemuelballebar1@gmail.com")
    print()
    print("=" * 60)
    print("IMPORTANT: Each value must be on a single line in .env")
    print("=" * 60)
    print()
    print(f"Public key length:  {len(public_key_b64)} chars (should be 87)")
    print(f"Private key length: {len(private_key_b64)} chars")

    if len(public_key_b64) == 87:
        print("\n✓ Public key length is correct for Web Push!")
    else:
        print(f"\n⚠ Unexpected public key length. Expected 87, got {len(public_key_b64)}")

if __name__ == '__main__':
    try:
        generate_vapid_keys()
    except ImportError as e:
        print(f"Missing dependency: {e}")
        print("Run: pip install pywebpush cryptography")
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
# 3. Object storage

The forum's uploads live here; `docs/05-forum-config.md` 5.6 covers the Discourse side.

**The endpoint** is Ceph RGW, answering S3 at `https://js2.jetstream-cloud.org:8001/` and Swift at `https://js2.jetstream-cloud.org:8001/swift/v1`. It addresses buckets by path only: there is no wildcard DNS and the certificate names only `js2.jetstream-cloud.org`, which is why the `qupa-s3-path-style` plugin exists.

**Credentials** are an EC2 credential, which the application credential can create:

```bash
openstack ec2 credentials create
openstack ec2 credentials delete <access>     # revoke
```

They go in `/etc/qupa-cafe/s3.env` on the host, mode 600, with LF line endings:

```ini
QUPA_S3_ACCESS_KEY_ID=<access>
QUPA_S3_SECRET_ACCESS_KEY=<secret>
QUPA_S3_BUCKET=qupa-cafe-uploads
QUPA_S3_ENDPOINT=https://js2.jetstream-cloud.org:8001
QUPA_S3_REGION=us-east-1
```

**The bucket** is created over Swift, which needs no request signing:

```bash
TOKEN=$(openstack token issue -f value -c id)
curl -X PUT -H "X-Auth-Token: $TOKEN" https://js2.jetstream-cloud.org:8001/swift/v1/qupa-cafe-uploads   # 201
curl -o /dev/null -w '%{http_code}\n' https://js2.jetstream-cloud.org:8001/qupa-cafe-uploads/              # 403
```

Buckets are private, and their names are unique across the whole cluster.

The certificate chain (InCommon via an emSign root) verifies on the Linux host and in the container's Ruby. `botocore` on Windows rejects it, because `certifi` lacks that root.

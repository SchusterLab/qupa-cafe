# Object storage

The forum's uploads live here. `docs/05-forum-config.md` covers the Discourse side; this is what the platform provides and what it takes to get at it.

## The endpoint

The service catalog lists `object-store`, Swift, at `https://js2.jetstream-cloud.org:8001/swift/v1`. It is Ceph RGW, so the same namespace answers the S3 API at `https://js2.jetstream-cloud.org:8001/` -- an unauthenticated `GET` there returns an S3 `ListAllMyBucketsResult` rather than a Swift error, which is the quickest way to tell.

**It addresses buckets by path only.** A request carrying `Host: <bucket>.js2.jetstream-cloud.org` is answered as though the first path segment were the bucket: signing the request Discourse would send and delivering it with that Host returned `NoSuchBucket` naming `original`, the leading segment of `original/1X/probe.pdf`. There is no wildcard DNS for a bucket subdomain and the endpoint's certificate carries one name, `js2.jetstream-cloud.org`, so the virtual-host form fails three ways over. This is what `plugins/qupa-s3-path-style` exists for.

That check needs no credentials, because the error names the bucket the server inferred:

```bash
curl https://<endpoint>/probe-b/key                 # NoSuchBucket: probe-b = path style
curl https://probe-a.<endpoint>/probe-b/key         # NoSuchBucket: probe-a = virtual host
```

## Credentials

S3 credentials come from Keystone as an EC2 credential:

```bash
openstack ec2 credentials create
```

The application credential in `app-cred-qupa-cafe-openrc.sh` can issue one through `POST /v3/users/<user>/credentials/OS-EC2`, which is not guaranteed -- application credentials are restricted by default. `openstack ec2 credentials delete <access>` revokes one, and issuing a replacement before revoking the old leaves a working credential if something fails in between.

The pair in use is in `/etc/qupa-cafe/s3.env` on the host, mode 600.

## Buckets

Either API can create one. Over Swift it needs no request signing, which is convenient from a machine whose Python does not trust the endpoint's certificate chain:

```bash
curl -X PUT -H "X-Auth-Token: $TOKEN" https://js2.jetstream-cloud.org:8001/swift/v1/<bucket>
```

Buckets are private: an unauthenticated listing answers 403, and so does an unauthenticated fetch of an object in one. Names are unique across the whole cluster rather than per project.

## The certificate chain

InCommon by way of an emSign root. The Linux host verifies it, and so does Ruby inside the container with `VERIFY_PEER` -- which is what matters, since that is the stack `aws-sdk-s3` uses. `botocore` on a Windows machine rejects it, because `certifi` does not carry that root while the Windows store does. That is a property of the client, not of the endpoint.

# Object storage, and why the forum does not use it

The allocation has object storage. Discourse cannot use it. Written down because the first four steps all succeed, so the dead end is only visible at the fifth.

## What is there

The service catalog lists `object-store`, Swift, at `https://js2.jetstream-cloud.org:8001/swift/v1`. It is Ceph RGW, so the same namespace answers the S3 API at `https://js2.jetstream-cloud.org:8001/` -- an unauthenticated `GET` there returns an S3 `ListAllMyBucketsResult` rather than a Swift error, which is the quickest way to tell.

S3 credentials come from Keystone as an EC2 credential:

```bash
openstack ec2 credentials create
```

The application credential in `app-cred-qupa-cafe-openrc.sh` was able to issue one through `POST /v3/users/<user>/credentials/OS-EC2`, which is not guaranteed -- application credentials are restricted by default.

A bucket can be created over either API. Over Swift it needs no request signing, which is convenient on a machine whose Python does not trust the endpoint's certificate chain:

```bash
curl -X PUT -H "X-Auth-Token: $TOKEN" https://js2.jetstream-cloud.org:8001/swift/v1/<bucket>
```

Buckets are private by default: an unauthenticated listing answers 403.

## Why Discourse cannot use it

Discourse builds S3 URLs virtual-host style and has no setting to change that, so with this endpoint it presigns:

```
https://<bucket>.js2.jetstream-cloud.org:8001/<key>
```

Measured, by asking the running container to presign one. That name has no DNS record, and the endpoint's certificate carries exactly one subject alternative name, `js2.jetstream-cloud.org`. It therefore fails in DNS, and would fail in TLS if it did not.

**And it would fail a third time if both were fixed.** This deployment has no `rgw_dns_name`, so it is path-style only: it takes the bucket from the first path segment and ignores the Host header. Signing the request Discourse would send and delivering it to the real endpoint with the bucket-prefixed `Host` -- so that only DNS and TLS were faked -- returned `NoSuchBucket` naming `original`, the first segment of `original/1X/probe.pdf`. So even wildcard DNS and a wildcard certificate would not make this endpoint serve Discourse.

That check needs no credentials anywhere, because the error names the bucket the server inferred:

```bash
curl https://probe-a.<endpoint>/probe-b/key    # NoSuchBucket: probe-a = virtual-host supported
                                               # NoSuchBucket: probe-b = path-style only
```

The SDK uses path-style when the endpoint is an IP address rather than a name, which is a real lever: presigning against `http://172.17.0.1:8002` produced `http://172.17.0.1:8002/<bucket>/<key>`. A proxy on the docker bridge forwarding to the real endpoint, `Host` header untouched so the signature still validates, would carry Discourse's own traffic.

It would not carry a researcher's. A secure upload is not streamed through Rails: `UploadsController` answers `redirect_to` a presigned URL, so that host has to be reachable and TLS-valid from a browser. The bridge address is not reachable from one, and the instance's public address has no certificate covering it.

## What that leaves

Nothing here, for uploads. `docs/05-forum-config.md` 5.6.1 states the requirement any provider has to meet, which is wildcard DNS on the endpoint plus a certificate covering it.

The certificate chain is worth one note for anything else that talks to this endpoint. It is InCommon by way of an emSign root: the Linux host verifies it (`Verify return code: 0`), and `botocore` on a Windows machine rejects it, because `certifi` does not carry that root while the Windows store does. That is a property of the client, not of the endpoint.

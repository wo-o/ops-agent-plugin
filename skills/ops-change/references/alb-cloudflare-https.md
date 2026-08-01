# ALB 도메인의 Cloudflare HTTPS 전환

## 핵심

DNS 레코드의 `proxied=true`는 Cloudflare edge를 활성화할 뿐, ALB origin의 HTTPS 구성을 대신하지 않는다. Cloudflare zone이 `Full` 또는 `Strict` SSL 모드라면 edge가 origin의 443으로 연결하므로, ALB에 HTTPS listener와 인증서가 없으면 클라이언트 HTTPS가 timeout 또는 52x로 실패할 수 있다.

## 안전한 검증 순서

1. 변경 전 현재 레코드와 ALB DNS를 조회한다.
2. ALB origin의 80과 443을 각각 확인한다.
   - `curl -i http://<alb-dns>/healthz`
   - `openssl s_client -connect <alb-dns>:443 -servername <domain> -brief </dev/null`
3. origin 443이 없으면 `proxied=true`만으로 HTTPS가 된다고 가정하지 않는다.
4. 변경 후 다음을 분리해서 확인한다.
   - Cloudflare 레코드가 `proxied=true`인지
   - `dig <domain>`이 Cloudflare edge IP를 반환하는지
   - `curl -i https://<domain>/healthz`가 실제로 200인지
   - `curl -i http://<domain>/healthz`도 필요한 동작을 하는지
5. apply가 성공했어도 HTTPS 검증이 실패하면 ⑤는 실패다. 사용하지 못하는 proxy 변경을 그대로 두지 말고, 기존 상태가 명확하면 tfvars PR로 원복한 뒤 원복 apply와 HTTP 정상 상태를 검증한다.

## surface 판단

- 기존 `<env>-dns` surface는 DNS record와 `proxied`만 다룬다.
- ACM certificate, ALB 443 HTTPS listener, certificate attachment, 80→443 redirect가 기존 writable surface에 없다면 신규 IaC 저작 범위 밖이다.
- 이 경우 필요한 리소스를 정리해 사람이 IaC 리포의 `.tf`를 작성해야 한다고 안내한다. Cloudflare `Flexible` 모드 같은 zone-wide 설정을 임의로 바꾸거나 직접 mutation하지 않는다.

## 보고 원칙

- PR/apply 성공과 실제 HTTPS 성공을 분리한다.
- 실패한 시도와 원복이 모두 있었다면 각 PR의 타임라인을 별도로 보고한다.
- `proxied=true`만 확인하고 “HTTPS 완료”라고 보고하지 않는다.

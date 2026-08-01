# dev 앱 업로드 파일용 private S3 버킷 템플릿.
# service_enabled 수명주기를 따라 서비스 삭제 시 버킷과 저장 객체도 함께 삭제된다.

data "aws_caller_identity" "app_uploads" {}

locals {
  app_uploads_project     = lower(var.project != "" ? var.project : basename(dirname(abspath(path.root))))
  app_uploads_bucket_name = "${local.app_uploads_project}-${var.environment}-app-uploads-${data.aws_caller_identity.app_uploads.account_id}"
}

resource "aws_s3_bucket" "app_uploads" {
  count         = var.service_enabled ? 1 : 0
  bucket        = local.app_uploads_bucket_name
  force_destroy = true

  tags = {
    Name    = local.app_uploads_bucket_name
    Service = "${local.app_uploads_project}-${var.environment}"
    Role    = "app-uploads"
  }
}

resource "aws_s3_bucket_public_access_block" "app_uploads" {
  count  = var.service_enabled ? 1 : 0
  bucket = aws_s3_bucket.app_uploads[0].id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "app_uploads" {
  count  = var.service_enabled ? 1 : 0
  bucket = aws_s3_bucket.app_uploads[0].id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "app_uploads" {
  count  = var.service_enabled ? 1 : 0
  bucket = aws_s3_bucket.app_uploads[0].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

output "app_uploads_bucket_name" {
  description = "dev 앱 업로드 파일용 S3 버킷 이름"
  value       = var.service_enabled ? aws_s3_bucket.app_uploads[0].id : null
}

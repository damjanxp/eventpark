# Image registry. In bootstrap (not envs/dev) so images survive the nightly `terraform destroy` of dev.

resource "aws_ecr_repository" "this" {
  for_each             = var.ecr_repositories
  name                 = each.value
  image_tag_mutability = "IMMUTABLE" # a tag (= git SHA) always means the same image

  image_scanning_configuration {
    scan_on_push = true # basic scanning: free
  }
}

resource "aws_ecr_lifecycle_policy" "this" {
  for_each   = aws_ecr_repository.this
  repository = each.value.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "keep the last ${var.keep_images} images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = var.keep_images
      }
      action = { type = "expire" }
    }]
  })
}

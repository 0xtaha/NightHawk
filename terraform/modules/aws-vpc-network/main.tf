data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  azs             = slice(data.aws_availability_zones.available.names, 0, var.az_count)
  public_subnets  = { for idx, az in local.azs : az => cidrsubnet(var.vpc_cidr, 4, idx) }
  private_subnets = { for idx, az in local.azs : az => cidrsubnet(var.vpc_cidr, 4, idx + var.az_count) }
  tags            = merge(var.tags, { ManagedBy = "terraform", Platform = "nighthawk" })
  sources         = toset([for rule in var.network_rules : rule.source])
  s3_rules        = { for rule in var.network_rules : rule.id => rule if rule.destination == "aws-s3" }
}

resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = merge(local.tags, { Name = "${var.name_prefix}-vpc" })
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = merge(local.tags, { Name = "${var.name_prefix}-igw" })
}

resource "aws_subnet" "public" {
  for_each                = local.public_subnets
  vpc_id                  = aws_vpc.this.id
  availability_zone       = each.key
  cidr_block              = each.value
  map_public_ip_on_launch = true
  # The role tag is how a load balancer integration finds subnets for internet-facing balancers.
  tags = merge(local.tags, {
    Name = "${var.name_prefix}-public-${each.key}", Tier = "public", "kubernetes.io/role/elb" = "1",
  })
}

resource "aws_subnet" "private" {
  for_each          = local.private_subnets
  vpc_id            = aws_vpc.this.id
  availability_zone = each.key
  cidr_block        = each.value
  # Likewise for internal balancers.
  tags = merge(local.tags, {
    Name = "${var.name_prefix}-private-${each.key}", Tier = "private", "kubernetes.io/role/internal-elb" = "1",
  })
}

resource "aws_eip" "nat" {
  domain = "vpc"
  tags   = merge(local.tags, { Name = "${var.name_prefix}-nat" })
}

# A single NAT gateway (in the first public subnet) is sufficient for the
# infrastructure this module provisions today; it is not a per-AZ HA NAT
# design. Revisit if a later phase needs AZ-independent NAT failure domains.
resource "aws_nat_gateway" "this" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.public[local.azs[0]].id
  tags          = merge(local.tags, { Name = "${var.name_prefix}-nat" })
  depends_on    = [aws_internet_gateway.this]
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id
  tags   = merge(local.tags, { Name = "${var.name_prefix}-public" })

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }
}

resource "aws_route_table_association" "public" {
  for_each       = aws_subnet.public
  subnet_id      = each.value.id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.this.id
  tags   = merge(local.tags, { Name = "${var.name_prefix}-private" })

  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.this.id
  }
}

resource "aws_route_table_association" "private" {
  for_each       = aws_subnet.private
  subnet_id      = each.value.id
  route_table_id = aws_route_table.private.id
}

# Private service connectivity to S3, matching the network contract's
# "aws-object-storage" rule purpose ("S3 via private service connectivity")
# instead of routing that traffic over public internet egress.
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${data.aws_region.this.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id, aws_route_table.public.id]
  tags              = merge(local.tags, { Name = "${var.name_prefix}-s3-endpoint" })
}

data "aws_region" "this" {}

resource "aws_security_group" "workload" {
  for_each    = local.sources
  name        = "${var.name_prefix}-${each.key}"
  description = "NightHawk ${each.key} security group derived from config/network.yaml"
  vpc_id      = aws_vpc.this.id
  tags        = merge(local.tags, { Name = "${var.name_prefix}-${each.key}" })
}

resource "aws_vpc_security_group_egress_rule" "s3" {
  for_each          = local.s3_rules
  security_group_id = aws_security_group.workload[each.value.source].id
  prefix_list_id    = aws_vpc_endpoint.s3.prefix_list_id
  ip_protocol       = each.value.protocol
  from_port         = each.value.port
  to_port           = each.value.port
  description       = each.value.id
}

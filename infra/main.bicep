// Neo4j Enterprise Edition - Azure Deployment Template
// Deploys Neo4j EE on Azure VM Scale Sets with optional load balancer for clusters

@description('Admin username for SSH access to VMs.')
param adminUsername string = 'neo4j'

@secure()
@description('Admin password for Neo4j VMs.')
param adminPassword string

param vmSize string

@description('Neo4j graph database version. Uses latest available from the stable yum repository.')
param graphDatabaseVersion string

param licenseType string = 'Enterprise'

@allowed([
  1
  3
  4
  5
  6
  7
  8
  9
  10
])
param nodeCount int

param diskSize int

param location string = resourceGroup().location

@description('OIDC configuration for M2M authentication (optional). Pass "none" to disable.')
param oidcConfig string = 'none'

@description('Fixed name for the Private Link Service. Stable across redeploys of the same resource group.')
param plsName string = 'pls-neo4j'

var deploymentUniqueId = uniqueString(resourceGroup().id, deployment().name)
var resourceSuffix = deploymentUniqueId

module network 'modules/network.bicep' = {
  name: 'network-deployment'
  params: {
    location: location
    resourceSuffix: resourceSuffix
  }
}

module identity 'modules/identity.bicep' = {
  name: 'identity-deployment'
  params: {
    location: location
    resourceSuffix: resourceSuffix
  }
}

var loadBalancerCondition = (nodeCount >= 3)

module loadbalancer 'modules/loadbalancer.bicep' = {
  name: 'loadbalancer-deployment'
  params: {
    location: location
    resourceSuffix: resourceSuffix
    loadBalancerCondition: loadBalancerCondition
    subnetId: network.outputs.subnetId
    plsSubnetId: network.outputs.plsSubnetId
    plsName: plsName
  }
}

// Cloud-init configuration for standalone and cluster deployments
var cloudInitStandalone = loadTextContent('cloud-init/standalone.yaml')
var cloudInitCluster = loadTextContent('cloud-init/cluster.yaml')

// Base64 encode the password for safe passing through cloud-init
// Note: This is for avoiding shell escaping issues, NOT for security/encryption
// The adminPassword parameter is already marked @secure() for encryption in deployment metadata
var passwordBase64 = base64(adminPassword)

// Primary cluster cloud-init processing (sequential variable assignments for readability)
var cloudInitTemplate = (nodeCount == 1) ? cloudInitStandalone : cloudInitCluster
var licenseAgreement = (licenseType == 'Evaluation') ? 'eval' : 'yes'
var cloudInitStep1 = replace(cloudInitTemplate, '\${unique_string}', deploymentUniqueId)
var cloudInitStep2 = replace(cloudInitStep1, '\${location}', location)
var cloudInitStep3 = replace(cloudInitStep2, '\${admin_password}', passwordBase64)
var cloudInitStep4 = replace(cloudInitStep3, '\${license_agreement}', licenseAgreement)
var cloudInitStep5 = replace(cloudInitStep4, '\${node_count}', string(nodeCount))
var cloudInitStep6 = replace(cloudInitStep5, '\${oidc_config}', oidcConfig)
var cloudInitData = cloudInitStep6
var cloudInitBase64 = base64(cloudInitData)

module vmss 'modules/vmss.bicep' = {
  name: 'vmss-deployment'
  params: {
    location: location
    resourceSuffix: resourceSuffix
    adminUsername: adminUsername
    adminPassword: adminPassword
    graphDatabaseVersion: graphDatabaseVersion
    licenseType: licenseType
    nodeCount: nodeCount
    vmSize: vmSize
    diskSize: diskSize
    cloudInitBase64: cloudInitBase64
    identityId: identity.outputs.identityId
    subnetId: network.outputs.subnetId
    loadBalancerBackendAddressPools: loadbalancer.outputs.loadBalancerBackendAddressPools
    loadBalancerCondition: loadBalancerCondition
  }
}

output vnetId string = network.outputs.vnetId
output subnetId string = network.outputs.subnetId
output nsgId string = network.outputs.nsgId
output identityId string = identity.outputs.identityId
output loadBalancerBackendAddressPools array = loadbalancer.outputs.loadBalancerBackendAddressPools
output lbPrivateIpAddress string = loadBalancerCondition ? loadbalancer.outputs.privateIpAddress : ''
output privateLinkServiceId string = loadbalancer.outputs.privateLinkServiceId
output vmScaleSetsId string = vmss.outputs.vmScaleSetsId
output vmScaleSetsName string = vmss.outputs.vmScaleSetsName

output Neo4jBrowserURL string = uri('http://vm0.neo4j-${deploymentUniqueId}.${location}.cloudapp.azure.com:7474', '')
output Username string = 'neo4j'

// SSH access information
output sshHostname string = 'vm0.neo4j-${deploymentUniqueId}.${location}.cloudapp.azure.com'
output sshUsername string = adminUsername
output sshCommand string = 'ssh ${adminUsername}@vm0.neo4j-${deploymentUniqueId}.${location}.cloudapp.azure.com'

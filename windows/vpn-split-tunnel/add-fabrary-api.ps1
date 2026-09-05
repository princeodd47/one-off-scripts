$domains = @(
    "content.fabrary.net",
    "42xrd23ihbd47fjvsrt27ufpfe.appsync-api.us-east-2.amazonaws.com",
    "cognito-identity.us-east-2.amazonaws.com",
    "cognito-idp.us-east-2.amazonaws.com"
)
& "$PSScriptRoot\split-tunnel.ps1" -Add -Domains $domains -Verify

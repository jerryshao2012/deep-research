# Deep Research handbook

This index is the canonical handbook navigation for installing, using, extending, and operating Deep Research.

## Getting started

- [Installation](getting-started/installation.md) explains prerequisites, dependency setup, and first-run model configuration.
- [Local development](getting-started/local-development.md) covers local servers, environment loading, and the developer workflow.
- [Usage](getting-started/usage.md) shows how to run research through the CLI or LangGraph server and stage documents through the Document Upload API.

## Guides

- [Configuration](guides/configuration.md) documents environment variables and model-provider settings.
- [Authentication](guides/authentication.md) explains API keys, OAuth, passkeys, and production hardening.
- [Reliability](guides/reliability.md) covers rate shaping, retries, and common operational failures.
- [Evaluation](guides/evaluation.md) describes regression tracking, verification, and evaluation metrics.

## API reference

- [Document Upload API](api/upload.md) documents document-staging and file-management endpoints for document-backed workflows.
- [Thread Wiki API](api/wiki.md) describes wiki generation, repository import, and related endpoints.
- [Postman collection](api/postman/README.md) explains how to configure and use the included API collection.

## Architecture

- [Architecture overview](architecture/overview.md) maps the major components and end-to-end data flow.
- [Clean Architecture](architecture/clean-architecture.md) describes application boundaries, dependency direction, and adapters.
- [Code ingestion](architecture/code-ingestion.md) explains AST-aware source ingestion and supported code paths.
- [Wiki diagram design](architecture/wiki-diagram-design.md) records the design behind the enhanced wiki architecture diagram.

## Deployment

- [AWS deployment](deployment/aws.md) explains container deployment through Amazon ECR and App Runner.
- [Vercel deployment](deployment/vercel.md) explains how a separately maintained companion frontend connects to and deploys in front of this backend.
- [Azure deployment](deployment/azure/README.md) explains the Container Apps architecture, prerequisites, deployment sequence, and health verification.
- [Azure storage](deployment/azure/storage.md) covers persistence, synchronization, migration, and rollback.
- [Azure operations](deployment/azure/operations.md) covers monitoring, scaling, networking, CI/CD, releases, and cost.
- [Azure security](deployment/azure/security.md) covers identity, secrets, network controls, authentication, and TLS.
- [Azure troubleshooting](deployment/azure/troubleshooting.md) collects deployment diagnostics and common failure recovery.

## Development

- [Testing](development/testing.md) documents the test hierarchy and verification commands.
- [Prompt validation](development/prompt-validation.md) covers validation tests, commands, assertions, and CI integration.
- [Extending the agent](development/extending-the-agent.md) explains how to add prompts, tools, skills, and model providers.

## Historical records

- [Implementation plans](history/plans/) preserve approved execution plans for completed and ongoing work.
- [Design specifications](history/specs/) preserve architecture and feature-design decisions.

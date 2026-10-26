---
name: tdd-workflow
description: Test-driven development enforcement patterns. Use when working on the TDD pipeline, debugging red/green/refactor cycle issues, or understanding how test-gen and dev phases interact.
---

# TDD Workflow Patterns

This skill covers Test-Driven Development enforcement in the Feature Factory pipeline. Load this skill when working on feature development or debugging TDD-related issues.

## Why TDD Matters

Agents will generate code. TDD ensures quality without manual review of every line:

1. **Quality gate**: Tests define expected behavior upfront
2. **Reviewability**: Easier to review tests than implementation
3. **Confidence**: Green tests = working code
4. **Self-verification**: Agents can validate their own work

## The TDD Cycle

```
+-------------------------------------------------------------+
|                                                             |
|   RED -------------> GREEN -------------> REFACTOR          |
|    |                  |                    |                 |
|    |                  |                    |                 |
|  Write              Write               Improve             |
|  failing            minimal             code                |
|  tests              code to             while               |
|                     pass                keeping             |
|                     tests               tests               |
|                                         green               |
|                                                             |
+-------------------------------------------------------------+
```

### Red Phase (test-gen agent)

Write tests that FAIL. This is critical - if tests pass before implementation, they're not testing new functionality.

**What test-gen does:**
1. Read specification from spec phase
2. Write unit tests for each function spec
3. Write integration tests for workflows
4. Run tests and VERIFY THEY FAIL
5. Report `testsCreated` count and `testStatus: 'failing'`

```typescript
// test-gen output
{
  testsCreated: 6,
  testFiles: [
    '__tests__/unit/handlers/register.test.js',
    '__tests__/integration/auth/registration-flow.test.js'
  ],
  testStatus: 'failing',  // REQUIRED: must be 'failing'
  failureMessages: [
    "Cannot find module '../../../src/handlers/register'",
    "TypeError: registerUser is not a function"
  ]
}
```

### Green Phase (dev agent)

Write MINIMAL code to make tests pass. No more, no less.

**What dev does:**
1. Verify tests exist and fail (via pre-phase hook)
2. Read test expectations
3. Write minimal implementation
4. Run tests until green
5. Commit working code

### Refactor Phase (optional, within dev)

Improve code quality while keeping tests green.

**What's allowed:**
- Rename for clarity
- Extract methods
- Remove duplication
- Improve performance

**What's NOT allowed:**
- Add new features
- Change behavior
- Break tests

## Pre-Phase Hooks

The workflow enforces TDD via hooks that run before each phase.

### tdd-enforcement Hook

Runs before `dev` phase. Blocks if TDD wasn't followed.

```typescript
// hooks/tdd-enforcement.ts
export async function tddEnforcementHook(
  context: WorkflowContext
): Promise<HookResult> {
  const testGenPhase = context.getPhaseResult('test-gen');

  // Verify test-gen completed
  if (!testGenPhase || testGenPhase.status !== 'completed') {
    return {
      success: false,
      error: 'TDD VIOLATION: test-gen phase must complete before dev'
    };
  }

  // Verify tests were created
  if (!testGenPhase.output?.testsCreated || testGenPhase.output.testsCreated === 0) {
    return {
      success: false,
      error: 'TDD VIOLATION: No tests created in test-gen phase'
    };
  }

  // Verify tests are FAILING (Red phase requirement)
  if (testGenPhase.output?.testStatus !== 'failing') {
    return {
      success: false,
      error: 'TDD VIOLATION: Tests must be failing before dev phase. ' +
             'If tests pass, they are not testing new functionality.'
    };
  }

  return { success: true };
}
```

### coverage-threshold Hook

Runs before `qa` phase. Ensures minimum coverage.

```typescript
// hooks/coverage-threshold.ts
export async function coverageThresholdHook(
  context: WorkflowContext
): Promise<HookResult> {
  const coverage = await runCoverageReport();

  if (coverage.lines < 80) {
    return {
      success: false,
      error: `Coverage threshold not met: ${coverage.lines}% < 80%`
    };
  }

  return { success: true };
}
```

## Workflow Configuration

Hooks are configured per-phase in workflow definitions:

```typescript
// workflows/new-feature.ts
export const newFeatureWorkflow: Workflow = {
  name: 'new-feature',
  phases: [
    { agent: 'architect', name: 'Architecture Review' },
    { agent: 'spec', name: 'Detailed Specification' },
    {
      agent: 'test-gen',
      name: 'TDD Red Phase',
      // No pre-hooks - test-gen starts the TDD cycle
    },
    {
      agent: 'dev',
      name: 'TDD Green Phase',
      prePhaseHooks: ['tdd-enforcement'],  // BLOCKS if tests don't fail
    },
    {
      agent: 'qa',
      name: 'Quality Assurance',
      prePhaseHooks: ['coverage-threshold'],  // BLOCKS if coverage < 80%
    },
    { agent: 'review', name: 'Code Review' },
    { agent: 'docs', name: 'Documentation' },
  ]
};
```

## TDD Violation Handling

When a hook fails, the workflow emits a `workflow-error` event and stops:

```typescript
// Workflow stops with error
{
  event: 'workflow-error',
  phase: 'dev',
  hook: 'tdd-enforcement',
  error: 'TDD VIOLATION: Tests must be failing before dev phase',
  resolution: 'Run test-gen again to create failing tests'
}
```

**To recover:**
1. Go back to test-gen phase
2. Write tests for the NEW functionality
3. Verify tests fail
4. Resume workflow

## Common TDD Pitfalls

### Pitfall 1: Tests Pass Before Implementation

**Problem**: Tests pass immediately, meaning they don't test new code.

**Cause**: Testing existing functionality or writing assertions that always pass.

**Fix**: Ensure tests reference NEW functions/features that don't exist yet.

### Pitfall 2: Over-Testing

**Problem**: Writing too many tests that overlap in coverage.

**Cause**: Testing implementation details instead of behavior.

**Fix**: Test PUBLIC interfaces and edge cases, not private methods.

### Pitfall 3: Under-Testing

**Problem**: Tests only cover happy path.

**Cause**: Skipping error cases to make tests pass faster.

**Fix**: spec phase should define error scenarios. Test them all.

### Pitfall 4: Modifying Tests to Pass

**Problem**: Changing test expectations to match buggy implementation.

**Cause**: Pressure to make tests green without fixing root cause.

**Fix**: Tests define CORRECT behavior. Fix implementation, not tests.

## Test Categories

| Category | Location | Runs When | Coverage Target |
|----------|----------|-----------|-----------------|
| Unit | `__tests__/unit/` | `npm test` | 80%+ |
| Integration | `__tests__/integration/` | `npm test` | Key flows |
| E2E | `__tests__/e2e/` | `npm run test:e2e` | Critical paths |

### Unit Tests

Test individual functions in isolation:

```typescript
describe('registerUser', () => {
  it('should create user with valid input', () => {
    const result = registerUser({ email: 'test@example.com', password: 'secure123' });
    expect(result.success).toBe(true);
    expect(result.user.email).toBe('test@example.com');
  });

  it('should require email', () => {
    expect(() => registerUser({ password: 'secure123' })).toThrow('Missing required: email');
  });

  it('should validate email format', () => {
    expect(() => registerUser({ email: 'not-an-email', password: 'secure123' }))
      .toThrow('Invalid email format');
  });
});
```

### Integration Tests

Test function interactions:

```typescript
describe('Registration flow', () => {
  it('should register and return auth token', async () => {
    // Call registration endpoint
    const registration = await testHandler('/api/register', {
      email: 'new@example.com',
      password: 'secure123'
    });
    expect(registration.status).toBe(201);
    expect(registration.body.token).toBeDefined();

    // Verify user can authenticate
    const auth = await testHandler('/api/login', {
      email: 'new@example.com',
      password: 'secure123'
    });
    expect(auth.status).toBe(200);
  });
});
```

### E2E Tests

Test with real services end-to-end:

```typescript
describe('User registration E2E', () => {
  it('should complete registration successfully', async () => {
    const response = await request(app)
      .post('/api/register')
      .send({ email: 'e2e@example.com', password: 'secure123' });

    expect(response.status).toBe(201);
    expect(response.body.user.id).toBeDefined();

    // Verify in database
    const user = await db.users.findByEmail('e2e@example.com');
    expect(user).toBeDefined();
    expect(user.status).toBe('active');
  });
});
```

## TDD and Agents

### test-gen Agent Responsibilities

1. **Read** spec from previous phase
2. **Identify** test scenarios from spec
3. **Write** test files with failing tests
4. **Run** tests to confirm failure
5. **Report** `testsCreated`, `testStatus: 'failing'`

### dev Agent Responsibilities

1. **Verify** tests fail (via hook)
2. **Read** test expectations
3. **Write** minimal implementation
4. **Run** tests iteratively
5. **Commit** when green

### review Agent Verification

Review agent checks TDD was followed:

```typescript
// Review checklist
{
  tddFollowed: {
    testsWrittenFirst: true,
    testsWereInitiallyFailing: true,
    implementationIsMinimal: true,
    noExtraFeatures: true
  }
}
```

## Related Documentation

- [Multi-agent patterns](/.claude/skills/multi-agent-patterns.md) - Orchestration patterns
- [TDD enforcement hooks](/.claude/skills/hooks-reference.md) - Hook implementation details

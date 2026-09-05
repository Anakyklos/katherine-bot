import assert from 'node:assert/strict';
import { test } from 'node:test';
import { existsSync, readFileSync, realpathSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const TESTS_ROOT = dirname(fileURLToPath(import.meta.url));
const FRONTEND_ROOT = join(TESTS_ROOT, '..');
const REPO_ROOT = join(FRONTEND_ROOT, '..');
const SKILLS_ROOT = join(REPO_ROOT, '.jcode', 'skills');

const SKILL_CONTRACTS = [
    {
        name: 'impeccable',
        revision: '8dac6ae7e020c43ab10ce9b41939f6fd42627b96',
        license: 'Apache License 2.0',
    },
    {
        name: 'emil-design-eng',
        revision: 'd23d7f88a2e21c9e4b1418c7abe420f5c1052ba7',
        license: 'MIT',
    },
];

test('project-local design skills are present with pinned provenance', () => {
    for (const skill of SKILL_CONTRACTS) {
        const skillRoot = join(SKILLS_ROOT, skill.name);
        const skillFile = join(skillRoot, 'SKILL.md');
        const provenanceFile = join(skillRoot, 'UPSTREAM.md');
        const licenseFile = join(skillRoot, 'LICENSE');

        assert.ok(existsSync(skillFile), `${skill.name} SKILL.md is missing`);
        assert.ok(existsSync(provenanceFile), `${skill.name} UPSTREAM.md is missing`);
        assert.ok(existsSync(licenseFile), `${skill.name} LICENSE is missing`);

        const skillSource = readFileSync(skillFile, 'utf8');
        const provenance = readFileSync(provenanceFile, 'utf8');
        assert.match(skillSource, new RegExp(`name:\\s*${skill.name}`));
        assert.match(provenance, new RegExp(skill.revision));
        assert.match(provenance, new RegExp(skill.license.replaceAll(' ', '\\s+')));
        assert.match(provenance, /project-local only/i);
        assert.match(provenance, /No global JCode skill installation is required/i);
        assert.equal(realpathSync(skillRoot).startsWith(realpathSync(SKILLS_ROOT)), true);
    }
});

test('the design skill gate is reproducible from workspace-local paths without a global install', () => {
    const localSkillPath = relative(REPO_ROOT, join(SKILLS_ROOT, 'impeccable', 'SKILL.md'));
    const localEmilPath = relative(REPO_ROOT, join(SKILLS_ROOT, 'emil-design-eng', 'SKILL.md'));

    assert.equal(localSkillPath, '.jcode/skills/impeccable/SKILL.md');
    assert.equal(localEmilPath, '.jcode/skills/emil-design-eng/SKILL.md');
    assert.doesNotMatch(localSkillPath, /^([/~]|[A-Za-z]:)/);
    assert.doesNotMatch(localEmilPath, /^([/~]|[A-Za-z]:)/);
});

test('bfce provenance is pinned to the approved canonical commit and license', () => {
    const vendorRoot = join(FRONTEND_ROOT, 'src', 'vendor', 'bfce');
    const provenance = readFileSync(join(vendorRoot, 'UPSTREAM.md'), 'utf8');
    const license = readFileSync(join(vendorRoot, 'LICENSE'), 'utf8');

    assert.match(provenance, /Requested upstream: https:\/\/github\.com\/bwndapp\/bfce/);
    assert.match(provenance, /Canonical upstream after permanent redirect: https:\/\/github\.com\/bwndapp\/bbot/);
    assert.match(provenance, /814e199c2045b3be057f59f8dc4ed395a4d2bbd6/);
    assert.match(provenance, /License: MIT/);
    assert.match(license, /MIT License/);
});

test('Katherine face CSS declares reduced-motion behavior', () => {
    const css = readFileSync(
        join(FRONTEND_ROOT, 'src', 'features', 'katherine-face', 'KatherineFace.css'),
        'utf8',
    );

    assert.match(css, /@media\s*\(prefers-reduced-motion:\s*reduce\)/);
    assert.match(css, /animation:\s*none\s*!important/);
    assert.match(css, /transition-duration:\s*0ms\s*!important/);
});

test('companion layout disables incidental disclosure motion for reduced-motion users', () => {
    const css = readFileSync(
        join(FRONTEND_ROOT, 'src', 'features', 'chat', 'components', 'CompanionLayout.css'),
        'utf8',
    );

    assert.match(css, /@media\s*\(prefers-reduced-motion:\s*reduce\)/);
    assert.match(css, /transition-duration:\s*0ms/);
});

"""Generate dependency declarations from installed packages and npm's lockfile."""
import importlib.metadata as metadata
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    rows = ['# 第三方依赖声明', '',
            'ObserveTrack 自身以 Apache-2.0 发布。以下依赖保留其原许可证，不因项目许可证而变更。',
            '此清单来自锁定依赖和本地安装元数据；完整原文汇总于 THIRD_PARTY_LICENSES.txt。',
            'Python 的 certifi 包使用 MPL-2.0；本项目未修改该包，分发其副本时须保留其许可证及源码获取权利。',
            '前端构建工具链的 lightningcss 及其平台包也采用 MPL-2.0，未作修改；构建工具本体不进入网页运行包。',
            '前端构建中可能包含依赖代码，分发构建产物时也应附带本声明与许可证原文。', '',
            '| 包 | 版本 | 许可证 |', '| --- | --- | --- |']
    texts = ['Third-party license texts. Copyright remains with each respective author.']
    for line in (ROOT / 'requirements.txt').read_text().splitlines():
        if '==' not in line or line.startswith('#'):
            continue
        name, version = line.split('==')
        dist = metadata.distribution(name)
        license_name = dist.metadata.get('License-Expression') or dist.metadata.get('License', '').split('\n')[0]
        if not license_name:
            license_name = '; '.join(c.split(' :: ')[-1] for c in dist.metadata.get_all('Classifier', []) if c.startswith('License ::'))
        rows.append(f'| Python: {name} | {version} | {license_name or "See license text"} |')
        for file in dist.files or []:
            basename = Path(str(file)).name.lower()
            if any(basename.startswith(word) for word in ['license', 'copying', 'notice']) and str(file).endswith(('.txt', '.md', 'LICENSE', 'COPYING', 'NOTICE')):
                path = Path(dist.locate_file(file))
                if path.is_file():
                    texts.append(f'\n===== {name} {version}: {basename} =====\n' + path.read_text(encoding='utf-8', errors='replace'))
    lock = json.loads((ROOT / 'frontend/package-lock.json').read_text())
    seen = set()
    for location, item in sorted(lock['packages'].items()):
        if not location:
            continue
        name = location.split('node_modules/')[-1]
        identity = name, item.get('version')
        if identity in seen:
            continue
        seen.add(identity)
        rows.append(f'| npm: {name} | {item.get("version", "")} | {item.get("license", "See package metadata")} |')
        folder = ROOT / 'frontend' / location
        if folder.is_dir():
            for path in sorted(folder.iterdir()):
                if path.is_file() and any(path.name.lower().startswith(word) for word in ['license', 'copying', 'notice']):
                    texts.append(f'\n===== {name} {item.get("version")}: {path.name} =====\n' + path.read_text(encoding='utf-8', errors='replace'))
    rows += ['', '开发工具 pip-audit 使用 Apache-2.0，仅用于检查，不作为应用运行组件分发。',
             '未在本机安装的其他平台可选构建包仍遵循锁文件中声明的许可证；其上游发布包携带原文。']
    (ROOT / 'THIRD_PARTY_NOTICES.md').write_text('\n'.join(rows) + '\n', encoding='utf-8')
    (ROOT / 'THIRD_PARTY_LICENSES.txt').write_text('\n'.join(texts) + '\n', encoding='utf-8')
    print('Generated third-party notices and license texts.')


if __name__ == '__main__':
    main()

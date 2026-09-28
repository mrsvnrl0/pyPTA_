from pathlib import Path
import ast
import importlib.util
import os
import sys
from PyInstaller.utils.hooks import collect_all

root = Path(SPECPATH).parent
sys.path.insert(0, str(root))
from tools.source_release import packaged_model_paths

flavor = os.environ['PYPTA_BUILD_FLAVOR']
protected = flavor == 'Obfuscated'
code = root/'obfuscated' if protected else root
ta_data, ta_bins, ta_hidden = collect_all('talib')
model_files = sorted(packaged_model_paths(root))
has_candidates = any('candidates' in path.relative_to(root).parts for path in model_files)
ort_data, ort_bins, ort_hidden = ([], [], [])
if has_candidates:
    if importlib.util.find_spec('onnxruntime') is None:
        raise RuntimeError('Candidate bundles require pinned onnxruntime in the Windows build environment')
    ort_data, ort_bins, ort_hidden = collect_all('onnxruntime')
package_modules = []
external_modules = set()
for module_path in [*(root/'adaptive_crypto').rglob('*.py'), root/'windows_launcher.py']:
    if module_path.name != 'windows_launcher.py':
        parts = list(module_path.relative_to(root).with_suffix('').parts)
        if parts[-1] == '__init__':
            parts.pop()
        package_modules.append('.'.join(parts))
    for node in ast.walk(ast.parse(module_path.read_text(encoding='utf-8-sig'))):
        if isinstance(node, ast.Import):
            external_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            external_modules.add(node.module)
hidden = ta_hidden + ort_hidden + ['numpy', 'pandas', 'talib', 'h5py', 'openai', 'certifi',
                     'tkinter.ttk', 'tkinter.messagebox'] + package_modules + sorted(external_modules)
if protected:
    hidden += [p.name for p in code.glob('pyarmor_runtime_*') if p.is_dir()]
datas = [(str(root/'adaptive_crypto'/name), 'adaptive_crypto/'+name)
         for name in ('templates', 'static')]
datas += [(str(path), path.parent.relative_to(root).as_posix()) for path in model_files]
datas += [(str(root/'packaging'/'licenses'), 'licenses')] + ta_data + ort_data
a = Analysis([str(code/'windows_launcher.py')], pathex=[str(code)], binaries=ta_bins + ort_bins,
             datas=datas, hiddenimports=hidden, hookspath=[], hooksconfig={}, runtime_hooks=[],
             excludes=['pytest', 'IPython', 'matplotlib', 'scipy'], noarchive=False, optimize=0)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='pyPTA', debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False, console=False,
          disable_windowed_traceback=False, icon=str(root/'packaging'/'dashboard.ico'),
          version=str(root/'packaging'/'version.txt'))
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='pyPTA-'+flavor)

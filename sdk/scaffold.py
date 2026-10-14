"""A new mod project from nothing: `bl new <Name>`.

Writes a folder that builds (`bl build`), installs (`bl deploy`) and can be test-run unattended (`bl run`):
  bl.json                                   the project file: module id, display name, version
  SubModule.xml                             the module manifest the launcher reads
  src/<Id>.csproj, SubModule.cs             the module's DLL: entry point and a log file
  src/AutoTest.cs                           the unattended test driver the harness talks to
  src/SceneSlots.cs                         scratch scene folders that can be filled while the game runs
  ModuleData/<id>_custom_battle_scenes.xml  the scenes of this module that Custom Battle lists
  .gitignore, README.md
The C# files come from sdk/scaffold/ with three names filled in. Nothing is read from the game.
"""
import json, os, re

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATES = os.path.join(HERE, "scaffold")
PROJECT_FILE = "bl.json"
NL = "\n"

SUBMODULE_XML = """<?xml version="1.0" encoding="utf-8"?>
<Module>
	<Name value="__NAME__"/>
	<Id value="__MOD__"/>
	<Version value="__VERSION__"/>
	<DefaultModule value="false"/>
	<ModuleCategory value="Singleplayer"/>
	<ModuleType value="Community"/>
	<DependedModules>
		<DependedModule Id="Native" DependentVersion="__GAMEVERSION__" Optional="false"/>
		<DependedModule Id="SandBoxCore" DependentVersion="__GAMEVERSION__" Optional="false"/>
		<DependedModule Id="CustomBattle" DependentVersion="__GAMEVERSION__" Optional="false"/>
		<DependedModule Id="Sandbox" DependentVersion="__GAMEVERSION__" Optional="true"/>
	</DependedModules>
	<SubModules>
		<SubModule>
			<Name value="__MOD__"/>
			<DLLName value="__MOD__.dll"/>
			<SubModuleClassType value="__MOD__.SubModule"/>
			<Tags/>
		</SubModule>
	</SubModules>
	<Xmls>
		<XmlNode>
			<XmlName id="CustomBattleScenes" path="__MODLOWER___custom_battle_scenes"/>
			<IncludedGameTypes>
				<GameType value="CustomGame"/>
			</IncludedGameTypes>
		</XmlNode>
	</Xmls>
</Module>
"""

CSPROJ = """<Project Sdk="Microsoft.NET.Sdk">
  <!-- bl build   (or: dotnet build -c Release -p:BannerlordDir="<game folder>" -o ../build) -->
  <PropertyGroup>
    <TargetFramework>net472</TargetFramework>
    <LangVersion>latest</LangVersion>
    <AssemblyName>__MOD__</AssemblyName>
    <RootNamespace>__MOD__</RootNamespace>
    <Nullable>disable</Nullable>
    <GenerateAssemblyInfo>false</GenerateAssemblyInfo>
    <AppendTargetFrameworkToOutputPath>false</AppendTargetFrameworkToOutputPath>
    <BannerlordDir Condition="'$(BannerlordDir)' == ''">C:\\Program Files (x86)\\Steam\\steamapps\\common\\Mount &amp; Blade II Bannerlord</BannerlordDir>
  </PropertyGroup>
  <ItemGroup>
    <PackageReference Include="Microsoft.NETFramework.ReferenceAssemblies" Version="1.0.3" PrivateAssets="all" />
  </ItemGroup>
  <ItemGroup>
    <Reference Include="$(BannerlordDir)\\bin\\Win64_Shipping_Client\\TaleWorlds.*.dll" Exclude="$(BannerlordDir)\\bin\\Win64_Shipping_Client\\TaleWorlds.Native*.dll;$(BannerlordDir)\\bin\\Win64_Shipping_Client\\TaleWorlds.Starter*.dll;$(BannerlordDir)\\bin\\Win64_Shipping_Client\\TaleWorlds.*Steam*.dll">
      <Private>false</Private>
    </Reference>
    <Reference Include="$(BannerlordDir)\\bin\\Win64_Shipping_Client\\netstandard.dll" Condition="Exists('$(BannerlordDir)\\bin\\Win64_Shipping_Client\\netstandard.dll')">
      <Private>false</Private>
    </Reference>
    <!-- MissionScreen (the test camera) lives in the View module, CustomGameManager (the test battle) in CustomBattle,
         SandBoxGameManager (the campaign test) in SandBox -->
    <Reference Include="$(BannerlordDir)\\Modules\\SandBox\\bin\\Win64_Shipping_Client\\SandBox.dll"><Private>false</Private></Reference>
    <Reference Include="$(BannerlordDir)\\Modules\\Native\\bin\\Win64_Shipping_Client\\TaleWorlds.MountAndBlade.View.dll"><Private>false</Private></Reference>
    <Reference Include="$(BannerlordDir)\\Modules\\CustomBattle\\bin\\Win64_Shipping_Client\\TaleWorlds.MountAndBlade.CustomBattle.dll"><Private>false</Private></Reference>
  </ItemGroup>
</Project>
"""

SCENES_XML = """<?xml version="1.0" encoding="utf-8"?>
<CustomBattleScenes>
</CustomBattleScenes>
"""

README = """# __NAME__

A Mount & Blade II: Bannerlord module, made with the Bannerlord Editor-Free SDK.

```powershell
bl build                      # compiles src/ into build/__MOD__.dll
bl deploy                     # copies the module into <game>\\Modules\\__MOD__ (game closed)
bl run --words "scene=<id> tod=12 fight secs=30 quit"     # one unattended test run
```

The module's log is `Modules\\__MOD__\\__MODLOWER___log.txt`.
"""

GITIGNORE = "build/\nobj/\nbin/\n*.dmp\n__pycache__/\n"


def valid_id(name): return bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{1,40}", name))


def files(mod, name=None, version="v0.1.0", game_version="v1.4.8"):
    """{relative path: text} of a new project."""
    if not valid_id(mod): raise ValueError("a module id is a letter followed by letters, digits or _ (2 to 41 characters): %r" % mod)
    rep = {"__MODLOWER__": mod.lower(), "__MOD__": mod, "__NAME__": name or mod, "__VERSION__": version, "__GAMEVERSION__": game_version}
    def fill(s):
        for k, v in rep.items(): s = s.replace(k, v)
        return s
    out = {PROJECT_FILE: json.dumps({"module": mod, "name": name or mod, "version": version, "game_version": game_version}, indent=2) + NL,
           "SubModule.xml": fill(SUBMODULE_XML), "src/%s.csproj" % mod: fill(CSPROJ),
           "ModuleData/%s_custom_battle_scenes.xml" % mod.lower(): SCENES_XML, "README.md": fill(README), ".gitignore": GITIGNORE}
    for fn in sorted(os.listdir(os.path.join(TEMPLATES, "src"))):
        if fn.endswith(".tpl"):
            with open(os.path.join(TEMPLATES, "src", fn), encoding="utf-8") as f: out["src/" + fn[:-4]] = fill(f.read())
    return out


def create(folder, mod, name=None, version="v0.1.0", game_version="v1.4.8", dry=False):
    """Writes the project into `folder` (which must be empty or absent). Returns the list of files."""
    fs = files(mod, name, version, game_version)
    if os.path.isdir(folder) and os.listdir(folder): raise ValueError("%s exists and is not empty" % folder)
    if not dry:
        for rel, text in fs.items():
            p = os.path.join(folder, rel); os.makedirs(os.path.dirname(p), exist_ok=True)
            # the game's own ModuleData XML files start with a byte order mark; the proven scene lists have one too
            with open(p, "w", encoding="utf-8-sig" if rel.startswith("ModuleData/") else "utf-8", newline=NL) as f: f.write(text)
    return sorted(fs)


def find_project(start="."):
    """The folder with bl.json at or above `start`, and its content; (None, None) when there is none."""
    d = os.path.abspath(start)
    while True:
        p = os.path.join(d, PROJECT_FILE)
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as f: return d, json.load(f)
        up = os.path.dirname(d)
        if up == d: return None, None
        d = up


KINDS = {"battle": "", "siege": ' is_siege_map="true"', "village": ' is_village_map="true"', "lords_hall": ' is_lords_hall_map="true"'}


def register_scene(project, mod, scene_id, name=None, terrain="Plain", remove=False, kind="battle"):
    """Adds a scene to (or removes it from) the project's Custom Battle list. Returns the ids listed afterwards.
    kind: battle, siege (a castle or town with walls), village, lords_hall: the flags Custom Battle sorts its maps by."""
    p = os.path.join(project, "ModuleData", "%s_custom_battle_scenes.xml" % mod.lower())
    text = open(p, encoding="utf-8-sig").read() if os.path.isfile(p) else SCENES_XML
    text = re.sub(r'[ \t]*<Scene\s+id="%s"[^>]*>(?:\s*</Scene>)?\r?\n?' % re.escape(scene_id), "", text)
    if not remove:
        # {=!} marks a name that has no translation id
        row = '\t<Scene id="%s" name="{=!}%s" terrain="%s"%s></Scene>\n' % (scene_id, (name or scene_id).replace('"', "'"), terrain, KINDS[kind])
        i = text.rindex("</CustomBattleScenes>"); text = text[:i] + row + text[i:]
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8-sig", newline=NL) as f: f.write(text)
    return re.findall(r'<Scene\s+id="([^"]+)"', text)


def add_prefab(project, mod, name, mesh=None, physics=None, scale=None, remove=False, skeleton=None):
    """Adds a prefab to (or removes it from) the project's Prefabs/<id>_prefabs.xml, the form the shipped modules use:
    a game_entity with an optional collision body and model. The game reads every Prefabs/*.xml of a module at startup.
    Returns the prefab names in the file afterwards."""
    p = os.path.join(project, "Prefabs", "%s_prefabs.xml" % mod.lower())
    text = open(p, encoding="utf-8").read() if os.path.isfile(p) else "<prefabs>\n</prefabs>\n"
    text = re.sub(r'[ \t]*<game_entity name="%s".*?</game_entity>\r?\n?' % re.escape(name), "", text, flags=re.S)
    if not remove:
        rows = ['\t<game_entity name="%s" old_prefab_name="">' % name]
        if scale is not None: rows.append('\t\t<transform position="0.000, 0.000, 0.000" rotation_euler="0.000, 0.000, 0.000" scale="%.3f, %.3f, %.3f"/>' % tuple(scale))
        if physics: rows.append('\t\t<physics shape="%s"/>' % physics)
        if mesh and skeleton:                                  # a skinned model hangs under the skeleton, as in the shipped banner prefabs
            rows += ['\t\t<skeleton skeleton_model="%s">' % skeleton, '\t\t\t<components>', '\t\t\t\t<meta_mesh_component name="%s"/>' % mesh, '\t\t\t</components>', '\t\t</skeleton>']
        elif mesh: rows += ['\t\t<components>', '\t\t\t<meta_mesh_component name="%s"/>' % mesh, '\t\t</components>']
        rows.append('\t</game_entity>')
        i = text.rindex("</prefabs>"); text = text[:i] + "\n".join(rows) + "\n" + text[i:]
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8", newline=NL) as f: f.write(text)
    return re.findall(r'<game_entity name="([^"]+)"', text)

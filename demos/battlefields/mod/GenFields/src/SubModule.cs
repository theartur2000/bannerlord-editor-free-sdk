// GenFields: the in-game half of the generated-battlefield tech demo.
//
// What it does today (steps F0 and F1 of docs/BATTLEFIELDS.md):
//   - wraps the campaign's SceneModel, the game model that turns a spot on the world map into a battle scene name;
//   - when Modules/GenFields/genfield.txt names a scene, every field battle goes to that scene;
//   - otherwise, when the data of the generator is installed (Live.cs), every field battle on land gets a field
//     generated at that moment from the map around the party (src/gen, about two seconds);
//   - otherwise, when a pool of generated fields is installed (Pool.cs), every field battle on land goes to a pool field
//     that fits the spot on the map; naval battles, sieges, villages and hideouts keep the game's own scenes;
//   - when that scene's folder is not in Modules/GenFields/SceneObj yet, it is copied there from the SDK's stage folder
//     right then, while the game runs. That is the test of "can the engine load a scene folder that did not exist at
//     startup";
//   - logs the map position, the map patch and the scene chosen for every battle to Modules/GenFields/genfields_log.txt.
// Scenes registered in ModuleData/genfields_custom_battle_scenes.xml also show up in the Custom Battle map list.
//
// What it does not do yet: generate anything. The scene folders come from demos/battlefields/generate.py.
using System;
using System.IO;
using System.Linq;
using TaleWorlds.CampaignSystem;
using TaleWorlds.CampaignSystem.ComponentInterfaces;
using TaleWorlds.CampaignSystem.Map;
using TaleWorlds.CampaignSystem.Party;
using TaleWorlds.Core;
using TaleWorlds.ModuleManager;
using TaleWorlds.MountAndBlade;

namespace GenFields
{
    public class SubModule : MBSubModuleBase
    {
        public const string ModuleId = "GenFields";

        protected override void OnSubModuleLoad()
        {
            base.OnSubModuleLoad();
            Log.Write("GenFields loaded");
            Live.EmptyPlaceholders();
            AutoTest.Load(ModuleHelper.GetModuleFullPath(ModuleId));
        }

        protected override void OnApplicationTick(float dt)
        {
            base.OnApplicationTick(dt);
            AutoTest.Tick(dt);
        }

        public override void OnMissionBehaviorInitialize(Mission mission)
        {
            base.OnMissionBehaviorInitialize(mission);
            if (AutoTest.On) mission.AddMissionBehavior(new AutoMissionLogic());
            try { if (Live.IsSlot(mission.SceneName) || Pool.Has(mission.SceneName)) mission.AddMissionBehavior(new FieldNoteLogic()); } catch (Exception e) { Log.Write("field note not added: " + e.Message); }
        }

        protected override void OnGameStart(Game game, IGameStarter gameStarter)
        {
            base.OnGameStart(game, gameStarter);
            try
            {
                if (!(gameStarter is CampaignGameStarter)) return;
                SceneModel inner = gameStarter.Models.OfType<SceneModel>().LastOrDefault();
                if (inner == null) { Log.Write("no SceneModel to wrap"); return; }
                gameStarter.AddModel(new GenFieldSceneModel(inner));
                Log.Write("scene model wraps " + inner.GetType().Name);
            }
            catch (Exception e) { Log.Write("OnGameStart failed: " + e); }
        }
    }

    public class GenFieldSceneModel : SceneModel
    {
        readonly SceneModel _inner;

        public GenFieldSceneModel(SceneModel inner) { _inner = inner; }

        public override string GetConversationSceneForMapPosition(CampaignVec2 campaignPosition)
            => _inner.GetConversationSceneForMapPosition(campaignPosition);

        public override string GetBattleSceneForMapPatch(MapPatchData mapPatch, bool isNavalEncounter)
        {
            string native = _inner.GetBattleSceneForMapPatch(mapPatch, isNavalEncounter);
            try
            {
                string where = "?";
                try { var p = MobileParty.MainParty.Position.ToVec2(); where = p.x.ToString("F2") + ", " + p.y.ToString("F2"); } catch { }
                string forced = isNavalEncounter ? null : Control.ForcedScene();
                if (forced == null && !isNavalEncounter && Live.On)
                {
                    string made = Live.Generate(Live.RulesHere());        // a new field for this battle, made now
                    if (made != null) { Log.Write("battle at map position " + where + ", native scene " + native + ", sent to the field just generated in " + made); return made; }
                }
                if (forced == null && !isNavalEncounter)
                {
                    string picked = Pool.Pick();        // a generated field that fits this spot of the map
                    if (picked != null) { Log.Write("battle at map position " + where + ", native scene " + native + ", sent to pool field " + picked); return picked; }
                }
                Log.Write("battle at map position " + where + ", patch index " + mapPatch.sceneIndex + " at "
                          + mapPatch.normalizedCoordinates.x.ToString("F3") + ", " + mapPatch.normalizedCoordinates.y.ToString("F3")
                          + ", native scene " + native + (forced != null ? ", sent to " + forced : ""));
                if (forced != null && Control.EnsureScene(forced)) return forced;
            }
            catch (Exception e) { Log.Write("GetBattleSceneForMapPatch failed: " + e); }
            return native;
        }
    }

    internal static class Control
    {
        static string ModulePath => ModuleHelper.GetModuleFullPath(SubModule.ModuleId);

        // the first non-empty line of genfield.txt that does not start with #, or null
        public static string ForcedScene()
        {
            string f = Path.Combine(ModulePath, "genfield.txt");
            if (!File.Exists(f)) return null;
            foreach (string line in File.ReadAllLines(f))
            {
                string s = line.Trim();
                if (s.Length > 0 && !s.StartsWith("#")) return s;
            }
            return null;
        }

        // the SDK's per-user stage folder (sdk/config.py): BANNERLORD_SDK_HOME, else %LOCALAPPDATA%\bannerlord-sdk
        static string StageScenes()
        {
            string home = Environment.GetEnvironmentVariable("BANNERLORD_SDK_HOME");
            if (string.IsNullOrEmpty(home)) home = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "bannerlord-sdk");
            return Path.Combine(home, "stage", "scenes");
        }

        // true when Modules/GenFields/SceneObj/<name> holds a scene; copies it from the stage folder when it is missing
        public static bool EnsureScene(string name)
        {
            string dst = Path.Combine(ModulePath, "SceneObj", name);
            if (File.Exists(Path.Combine(dst, "scene.xscene"))) return true;
            string src = Path.Combine(StageScenes(), name);
            if (!File.Exists(Path.Combine(src, "scene.xscene"))) { Log.Write("scene " + name + " is neither installed nor staged at " + src); return false; }
            var t0 = DateTime.UtcNow;
            CopyDir(src, dst);
            Log.Write("scene " + name + " copied into the module while the game runs (" + (DateTime.UtcNow - t0).TotalMilliseconds.ToString("F0") + " ms)");
            return true;
        }

        // the files of the staged scene `from` written over the installed scene folder `into` (which the engine knew at startup);
        // the scene's own name inside scene.xscene is changed to the folder's
        public static bool Overwrite(string from, string into)
        {
            string src = Path.Combine(StageScenes(), from), dst = Path.Combine(ModulePath, "SceneObj", into ?? "");
            if (into == null || !File.Exists(Path.Combine(src, "scene.xscene")) || !File.Exists(Path.Combine(dst, "scene.xscene")))
            { Log.Write("swap: need a staged scene " + src + " and an installed scene " + dst); return false; }
            var t0 = DateTime.UtcNow;
            CopyDir(src, dst);
            string x = Path.Combine(dst, "scene.xscene");
            string text = File.ReadAllText(x);
            int i = text.IndexOf("<scene name=\"" + from + "\"", StringComparison.Ordinal);
            if (i >= 0) File.WriteAllText(x, text.Substring(0, i) + "<scene name=\"" + into + "\"" + text.Substring(i + ("<scene name=\"" + from + "\"").Length));
            Log.Write("swap: files of " + from + " written over installed scene " + into + " while the game runs (" + (DateTime.UtcNow - t0).TotalMilliseconds.ToString("F0") + " ms)");
            return true;
        }

        static void CopyDir(string src, string dst)
        {
            Directory.CreateDirectory(dst);
            foreach (string f in Directory.GetFiles(src)) File.Copy(f, Path.Combine(dst, Path.GetFileName(f)), true);
            foreach (string d in Directory.GetDirectories(src)) CopyDir(d, Path.Combine(dst, Path.GetFileName(d)));
        }
    }

    static class Log
    {
        static readonly object Gate = new object();

        public static void Write(string text)
        {
            try
            {
                lock (Gate)
                    File.AppendAllText(Path.Combine(ModuleHelper.GetModuleFullPath(SubModule.ModuleId), "genfields_log.txt"),
                                       DateTime.Now.ToString("HH:mm:ss.fff") + " " + text + Environment.NewLine);
            }
            catch { }
        }
    }
}

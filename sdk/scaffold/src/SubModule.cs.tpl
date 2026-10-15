// __NAME__: the module's entry point.
//
// The game calls the overrides below. What is here already: a log file (Modules/__MOD__/__MODLOWER___log.txt), the
// unattended test driver (AutoTest.cs), the scratch scene slots (SceneSlots.cs) and the bridge that takes commands from
// outside while the game runs (Bridge.cs). Put the mod's own code in new files and hook it in where the comments say.
using System;
using System.IO;
using System.Text;
using TaleWorlds.Core;
using TaleWorlds.ModuleManager;
using TaleWorlds.MountAndBlade;

namespace __MOD__
{
    public class SubModule : MBSubModuleBase
    {
        public const string ModuleId = "__MOD__";
        public static string ModulePath => ModuleHelper.GetModuleFullPath(ModuleId);

        protected override void OnSubModuleLoad()
        {
            base.OnSubModuleLoad();
            Log.Write("__MOD__ loaded");
            MapTiles.Fix();
            SceneSlots.EmptyUsed();
            AutoTest.Load(ModulePath);
        }

        protected override void OnApplicationTick(float dt)
        {
            base.OnApplicationTick(dt);
            AutoTest.Tick(dt);
            Bridge.Tick(dt);
        }

        public override void OnMissionBehaviorInitialize(Mission mission)
        {
            base.OnMissionBehaviorInitialize(mission);
            if (AutoTest.On) mission.AddMissionBehavior(new AutoMissionLogic());
            // the mod's own mission behaviours go here
        }

        protected override void OnGameStart(Game game, IGameStarter gameStarter)
        {
            base.OnGameStart(game, gameStarter);
            // campaign models and behaviours go here: if (gameStarter is CampaignGameStarter starter) { ... }
        }
    }

    // A campaign map copy (SceneObj/Main_map, written by `bl worldmap ...`) draws the shipped map's colours through a 4 MB
    // WorldMap.gts whose page names are paths to SandBox's page files, relative to the .gts folder. That path depends on
    // where the module is installed (Modules or a Steam Workshop folder): it is set right here, before any map loads.
    static class MapTiles
    {
        const int PagesAt = 0xAAD38, Slot = 0x218, NameBytes = 0x200;   // page name slots of WorldMap.gts (game v1.4.8)

        public static void Fix()
        {
            try
            {
                string tiles = Path.Combine(SubModule.ModulePath, "SceneObj", "Main_map", "TileSets");
                string gts = Path.Combine(tiles, "WorldMap.gts");
                if (!File.Exists(gts)) return;
                string pages = Path.Combine(ModuleHelper.GetModuleFullPath("Sandbox"), "SceneObj", "Main_map", "TileSets", "pages");
                string prefix = Relative(tiles, pages) + "/";
                byte[] b = File.ReadAllBytes(gts); bool changed = false;
                for (int k = 0; k < 32; k++)
                {
                    int o = PagesAt + k * Slot;
                    string name = Encoding.Unicode.GetString(b, o, NameBytes).Split('\0')[0];
                    if (!name.EndsWith(".gtp")) break;
                    string want = prefix + name.Substring(name.LastIndexOf('/') + 1);
                    if (want == name) continue;
                    byte[] w = Encoding.Unicode.GetBytes(want);
                    if (w.Length + 2 > NameBytes) throw new Exception("page path too long: " + want);
                    Array.Clear(b, o, NameBytes); Buffer.BlockCopy(w, 0, b, o, w.Length); changed = true;   // the page checksum after the name stays
                }
                if (changed) { File.WriteAllBytes(gts, b); Log.Write("map tile set: pages at " + prefix); }
            }
            catch (Exception e) { Log.Write("map tile set not set up: " + e.Message); }
        }

        static string Relative(string from, string to)
        {
            var f = new Uri(Path.GetFullPath(from).TrimEnd('\\', '/') + "/"); var t = new Uri(Path.GetFullPath(to).TrimEnd('\\', '/') + "/");
            if (f.IsFile && t.IsFile && !string.Equals(Path.GetPathRoot(f.LocalPath), Path.GetPathRoot(t.LocalPath), StringComparison.OrdinalIgnoreCase))
                throw new Exception("the module and SandBox are on different drives");
            return Uri.UnescapeDataString(f.MakeRelativeUri(t).ToString()).TrimEnd('/');
        }
    }

    // One line per call, with the time, flushed at once: the harness reads this file while the game runs.
    static class Log
    {
        static readonly object Gate = new object();

        public static void Write(string line)
        {
            try
            {
                lock (Gate) File.AppendAllText(Path.Combine(SubModule.ModulePath, "__MODLOWER___log.txt"), DateTime.Now.ToString("HH:mm:ss.fff") + " " + line + Environment.NewLine);
            }
            catch { }
        }
    }
}

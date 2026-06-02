using System;
using System.Collections;
using System.IO;
using System.Reflection;
using System.Xml;

internal static class VistaGuideImport
{
    private static string _logPath;

    private static int Main(string[] args)
    {
        AppDomain.CurrentDomain.UnhandledException += delegate(object sender, UnhandledExceptionEventArgs e)
        {
            Log("UNHANDLED: " + (e.ExceptionObject == null ? "<null>" : e.ExceptionObject.ToString()));
        };
        try
        {
            if (args.Length == 2 && args[0] == "--validate")
            {
                ValidateXml(args[1]);
                Console.WriteLine("Vista guide XML validation passed.");
                return 0;
            }
            if (args.Length != 2)
            {
                Console.Error.WriteLine("Usage: HDHRProxyWMC-VistaGuideImport.exe guide.xml log.txt");
                return 2;
            }

            _logPath = args[1];
            string xmlPath = Path.GetFullPath(args[0]);
            Log("Helper process started. Runtime=" + Environment.Version + " OS=" + Environment.OSVersion);
            Log("Validating Vista guide XML: " + xmlPath);
            ValidateXml(xmlPath);
            Log("Vista guide XML validation passed.");
            Log("Starting Vista ehepg import: " + xmlPath);

            string ehome = Path.Combine(Environment.GetEnvironmentVariable("WINDIR") ?? @"C:\Windows", "ehome");
            Log("Vista eHome folder: " + ehome);
            Assembly ehepg = LoadEhepg(ehome);
            Hashtable resolving = new Hashtable();
            AppDomain.CurrentDomain.AssemblyResolve += delegate(object sender, ResolveEventArgs e)
            {
                string simpleName = new AssemblyName(e.Name).Name;
                if (resolving.ContainsKey(simpleName))
                {
                    Log("Skipping recursive dependency resolution: " + e.Name);
                    return null;
                }
                resolving[simpleName] = true;
                try
                {
                    Log("Resolving dependency: " + e.Name);
                    string candidate = Path.Combine(ehome, simpleName + ".dll");
                    if (File.Exists(candidate))
                    {
                        Log("Loading dependency from eHome: " + candidate);
                        return Assembly.LoadFrom(candidate);
                    }
#pragma warning disable 618
                    Assembly registered = Assembly.LoadWithPartialName(simpleName);
#pragma warning restore 618
                    Log(registered == null
                        ? "Registered dependency was not found: " + e.Name
                        : "Loaded registered dependency: " + registered.FullName);
                    return registered;
                }
                finally
                {
                    resolving.Remove(simpleName);
                }
            };

            Log("Resolving required Vista ehepg types.");
            Type security = RequireType(ehepg, "Microsoft.Ehome.Epg.Helper.EpgSecurity");
            Type fileHelper = RequireType(ehepg, "Microsoft.Ehome.Epg.Helper.EpgFileHelper");
            Type managerType = RequireType(ehepg, "Microsoft.Ehome.Epg.Loader.GuideLoadManager");
            Log("Required Vista ehepg types resolved.");

            string encryptedPath = Path.Combine(Path.GetTempPath(), "hdhrproxy-" + Guid.NewGuid().ToString("N") + ".sdf");
            try
            {
                bool encrypted = (bool)security.GetMethod(
                    "EncryptFile",
                    BindingFlags.Public | BindingFlags.Static
                ).Invoke(null, new object[] { xmlPath, encryptedPath });
                if (!encrypted)
                    throw new InvalidOperationException("Vista EpgSecurity.EncryptFile returned false.");
                Log("Encrypted Vista guide XML: " + encryptedPath);

                Log("Resolving Vista current EPG database.");
                string databasePath = (string)fileHelper.GetProperty(
                    "CurrentEpgFile",
                    BindingFlags.Public | BindingFlags.Static
                ).GetValue(null, null);
                if (String.IsNullOrEmpty(databasePath))
                    throw new InvalidOperationException("Vista EpgFileHelper.CurrentEpgFile returned no database path.");
                Log("Vista current EPG database: " + databasePath);

                Log("Creating Vista GuideLoadManager.");
                ConstructorInfo managerCtor = managerType.GetConstructors(
                    BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance
                )[0];
                object manager = managerCtor.Invoke(new object[] { null });
                Log("Calling Vista GuideLoadManager.LoadXmlFile.");
                MethodInfo loadXml = managerType.GetMethod(
                    "LoadXmlFile",
                    BindingFlags.Public | BindingFlags.Instance
                );
                bool loaded = (bool)loadXml.Invoke(manager, new object[] { encryptedPath, databasePath });
                if (!loaded)
                    throw new InvalidOperationException("Vista GuideLoadManager.LoadXmlFile returned false.");

                Log("Vista ehepg import completed successfully.");
                return 0;
            }
            finally
            {
                try { if (File.Exists(encryptedPath)) File.Delete(encryptedPath); } catch { }
            }
        }
        catch (Exception ex)
        {
            Log("FATAL: " + ex);
            Console.Error.WriteLine(ex);
            return 1;
        }
    }

    private static void ValidateXml(string path)
    {
        XmlDocument doc = new XmlDocument();
        doc.Load(path);
        XmlElement root = doc.DocumentElement;
        if (root == null || root.Name != "epg_data")
            throw new InvalidDataException("Vista guide XML must start with epg_data.");

        RequireSection(root, "copyright");
        XmlElement tuneRequests = RequireSection(root, "tuneRequests");
        RequireCount(tuneRequests, "tr");
        RequireSection(root, "categories");
        RequireSection(root, "programAttributes");
        RequireSection(root, "programRatingAttributes");
        RequireSection(root, "scheduleEntryAttributes");
        XmlElement programs = RequireSection(root, "programs");
        RequireCount(programs, "p");
        RequireSection(root, "scheduleEntries");
        RequireSection(root, "scheduleEntries2");
    }

    private static XmlElement RequireSection(XmlElement root, string name)
    {
        XmlElement section = root[name];
        if (section == null)
            throw new InvalidDataException("Vista guide XML is missing section: " + name);
        return section;
    }

    private static void RequireCount(XmlElement section, string childName)
    {
        int expected;
        if (!Int32.TryParse(section.GetAttribute("ct"), out expected))
            throw new InvalidDataException(section.Name + " is missing a valid ct attribute.");
        if (section.GetElementsByTagName(childName).Count != expected)
            throw new InvalidDataException(section.Name + " ct does not match its child count.");
    }

    private static Type RequireType(Assembly assembly, string name)
    {
        Type type = assembly.GetType(name, false);
        if (type == null)
            throw new TypeLoadException("Missing Vista ehepg type: " + name);
        return type;
    }

    private static Assembly LoadEhepg(string ehome)
    {
        try
        {
            Log("Attempting strong-name Vista ehepg GAC load.");
            Assembly registered = Assembly.Load(
                "ehepg, Version=6.0.6000.0, Culture=neutral, PublicKeyToken=31bf3856ad364e35"
            );
            Log("Loaded registered Vista ehepg assembly: " + registered.FullName);
            return registered;
        }
        catch (Exception ex)
        {
            Log("Strong-name ehepg assembly load failed: " + ex.Message);
        }

        try
        {
            Log("Attempting registered Vista ehepg assembly load.");
#pragma warning disable 618
            Assembly registered = Assembly.LoadWithPartialName("ehepg");
#pragma warning restore 618
            if (registered != null)
            {
                Log("Loaded registered Vista ehepg assembly: " + registered.FullName);
                return registered;
            }
        }
        catch (Exception ex)
        {
            Log("Registered ehepg assembly load failed: " + ex.Message);
        }

        string path = Path.Combine(ehome, "ehepg.dll");
        if (File.Exists(path))
        {
            Log("Loading Vista ehepg assembly from: " + path);
            return Assembly.LoadFrom(path);
        }

        throw new FileNotFoundException(
            "Vista ehepg assembly was not found in the GAC or C:\\Windows\\ehome. " +
            "ehepgnet.dll alone is not the guide-store assembly.",
            path
        );
    }

    private static void Log(string message)
    {
        try
        {
            if (!String.IsNullOrEmpty(_logPath))
                File.AppendAllText(_logPath, "[VistaGuideImport] " + message + Environment.NewLine);
        }
        catch { }
    }
}

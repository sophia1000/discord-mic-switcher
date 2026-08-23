using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;

const int size = 256;
string outputPath = Path.GetFullPath(args.Length > 0 ? args[0] : Path.Combine("assets", "SophiasMicBridge.ico"));
Directory.CreateDirectory(Path.GetDirectoryName(outputPath)!);

using var bitmap = new Bitmap(size, size, PixelFormat.Format32bppArgb);
using (var graphics = Graphics.FromImage(bitmap))
{
    graphics.SmoothingMode = SmoothingMode.AntiAlias;
    graphics.InterpolationMode = InterpolationMode.HighQualityBicubic;

    using var background = new LinearGradientBrush(new Rectangle(0, 0, size, size), Color.FromArgb(8, 12, 18), Color.FromArgb(25, 33, 54), 45);
    FillRoundedRectangle(graphics, background, new Rectangle(4, 4, 248, 248), 52);

    using var border = new Pen(Color.FromArgb(84, 102, 133), 5);
    graphics.DrawPath(border, RoundedRectangle(new Rectangle(6, 6, 244, 244), 50));

    using var glow = new SolidBrush(Color.FromArgb(35, 45, 212, 191));
    graphics.FillEllipse(glow, 24, 26, 208, 208);

    using var rail = new Pen(Color.FromArgb(99, 102, 241), 13) { StartCap = LineCap.Round, EndCap = LineCap.Round };
    graphics.DrawLine(rail, 47, 179, 209, 179);
    using var node = new SolidBrush(Color.FromArgb(139, 92, 246));
    graphics.FillEllipse(node, 33, 165, 28, 28);
    graphics.FillEllipse(node, 195, 165, 28, 28);

    using var mic = new SolidBrush(Color.FromArgb(45, 212, 191));
    FillRoundedRectangle(graphics, mic, new Rectangle(92, 50, 72, 102), 36);
    using var stem = new Pen(Color.FromArgb(45, 212, 191), 15) { StartCap = LineCap.Round, EndCap = LineCap.Round };
    graphics.DrawLine(stem, 128, 152, 128, 184);
    graphics.DrawLine(stem, 101, 188, 155, 188);

    using var support = new Pen(Color.FromArgb(226, 232, 240), 12) { StartCap = LineCap.Round, EndCap = LineCap.Round };
    graphics.DrawArc(support, 70, 95, 116, 102, 20, 140);

    using var accent = new Pen(Color.FromArgb(244, 63, 94), 15) { StartCap = LineCap.Round, EndCap = LineCap.Round };
    graphics.DrawLine(accent, 67, 64, 190, 187);
}

using var png = new MemoryStream();
bitmap.Save(png, ImageFormat.Png);
png.Position = 0;
using var file = File.Create(outputPath);
using var writer = new BinaryWriter(file);
writer.Write((ushort)0);
writer.Write((ushort)1);
writer.Write((ushort)1);
writer.Write((byte)0);
writer.Write((byte)0);
writer.Write((byte)0);
writer.Write((byte)0);
writer.Write((ushort)1);
writer.Write((ushort)32);
writer.Write((uint)png.Length);
writer.Write((uint)22);
png.CopyTo(file);

static GraphicsPath RoundedRectangle(Rectangle bounds, int radius)
{
    int diameter = radius * 2;
    var path = new GraphicsPath();
    path.AddArc(bounds.Left, bounds.Top, diameter, diameter, 180, 90);
    path.AddArc(bounds.Right - diameter, bounds.Top, diameter, diameter, 270, 90);
    path.AddArc(bounds.Right - diameter, bounds.Bottom - diameter, diameter, diameter, 0, 90);
    path.AddArc(bounds.Left, bounds.Bottom - diameter, diameter, diameter, 90, 90);
    path.CloseFigure();
    return path;
}

static void FillRoundedRectangle(Graphics graphics, Brush brush, Rectangle bounds, int radius)
{
    using var path = RoundedRectangle(bounds, radius);
    graphics.FillPath(brush, path);
}

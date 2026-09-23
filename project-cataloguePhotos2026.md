**Project: Catalogue Photos**

**Problem:&#32;&#32;**

Over the past 2 decades at least I have taken many photos with various cameras.   Most of these made their way onto external hard drives and NAS and USB drives over the years.  Many of these made their photos are duplicates and many are garbage and should have been deleted when they were taken.  

The foundational principle was that this MUST be all local and be able to run without network connectivity,  No Internet and no Hosted LLM models.  Total Local.

Also, time is not an issue…mostly.  I don’t care if this has to run for days to complete ( I have nothing but time ) but it has to be doing something, moving in the right direction 


**Proposed solution:**

Build a AI agent that will find/discover all the pictures of all types on all (connected) hard drives file systems, analyze them for contents and categorize them with tags for the major features ( up to 4 tags ).  Once the analysis is done then either move the files to an appropriate directory for all similar photos ( eg. all bird photos together and all Ammo photos together etc ).  This idea Of moving the photos was quickly discovered as stupid.  It would mean various years and places would all be in the same directory and that there would be massive amounts of file IO.  Also, the weighting of which tag has primacy would be subjective, or the same photo would end up in multiple places - more duplication.

The subsequent solution is to build a CSV file listing metadata for all the photos.  Name, Location, Size, Tags, and LLM Description for every photo.  This proved the right thing to do.

**Exclusions**:   

1. Phone photos in iCloud.  Approx 8000 today.   These will be transferred to a file system locally eventually and then catalogued.  
2. Videos of all formats.  These can come later but keeping it simple right now.  Video descriptions will be tricky as tagging its true meaning/intent may be tricky.  


**False Starts**

The initial idea was to have an LLM review and describe a photo.  I was already working with a number of AI LLM Agent harnesses and encountering failures and some successes.  

The hardware is narrowed from Jake as it was the only computer with a GPU (RTX 570) and 12 gig RAM.  Running on Ubuntu proved a bit tricky as the drivers were not exactly right.  Sam was able to get things working using Radeon and Vulcan drivers.

Once Ollama was working on the GPU with models under 9 gig in memory consumption i started with OpenClaw, then moved to ZeroClaw, then NanoClaw, then Nous… and finally Hermes. The Ollama model was always responsive but the various harness and agents added so much additional prompt overhead (33k in some cases) that the context window was rapidly consumed and responses took 7-10 minutes.  Sam was also able to solve that problem by generating his own model from an existing model with a tweaked ModelFile that has a smaller size and larger Context window.  Hermes worked great and was usable via telegram bot. 

The agents still posed issues in analyzing the image files - 68 sample files of puffins and people.  It claimed randomly that it couldn’t access the files, the visual\_analyze tools, or claimed everything worked perfectly but actually failed to execute the requested actions.  Bottom line, agents are unreliable when asking for them to take any actions.

Sam suggested direct API use to the model and provided a python script example that worked and provided rough numeric output.  I manually coded a loop to scan through multiple files in a directory recursively.  Thus began the decent into Torch Vision learning.  wow.  That is a powerful and complex project.  Too much overhead but it could certainly manage this project.  I just didn’t have the brainpower to commit to going down that road right now.  Save that for a future effort

**Current Effort.&#32;**

Enter Claude.  i provided this prompt to a free version of Claude from Anthropic:

*I have many thousands of photos on hard drives that I want Claude to categorize (eg birds, people, motorcycles, plants, etc) and build a database of location,name,size and content of the images.  How would you go about completing this task?  Time is not an issue, it can go slowly.   How much would it cost ?*

It responded in detail and with the cost estimates of $12USD for 10k images processed and $125USD for 100k.  This assumes that it does the work. As this is running on all local elements the only cost is electricity.  It also provided the categorize\_photos.py script that includes sufficient comments to get the system configured and dependancies installed.  It ran correctly the first time executed.

It recommended two Models for the ollama local model that are weighted for image analysis.  Both were better than gemma4 or QWen3 i was previously using.  MoonDream is smaller and less tolerant of complex images ( most ) but LLava is the right balance of accuracy and size for this hardware.

The resulting file is a SQLite3 database that has tables for tags, locations, and photos.  The descriptions are short and accurate In the photos table and there are sample SQL statements in the script comments.

Since SQLite is now able to scale to 278 terabytes of dataDave file size it should not have any limits for this projects.

The Next steps after testing the Claude generated script on 50 and then 500 photos was to build a presentation layer for the database of information.  Back to the free version of Claude…

*For the same Ubuntu computer please provide the steps and code necessary to set up a web site/server that presents the clients with a page representing the contents of the photos.db sqlite3 database.   The web pages show show the picture by link and the ability to mark then for removal (but not actually delete anything). The page should also provide a way to add new user define tags and allow the user to update the description sentence.  Since this computer will be restricted to one internal network auth is not required right now.  Ask any questions you need to to build this site*

 The result was a Flask app that includes 4 files, and index.html, app.py, Style css and an app.js file to run locally.  Again, it ran perfectly the first time executed.  I was blown away.  It also included metadata on GPS and EXIF info if available (my DSLRs typically never added that info)



The description can be updated, new tags added and a flag set to delete files that the user deems needing deletion.  

**Processing Begins**

Now that the solution is operational I decided that just plugging in all the USB drives and leaving the photos where they were was not going to work. Once the drive was unplugged or moved to another port it might never be addressable again through the database As the ‘location’ would have changed.  That meant moving all the files to the local machine.  good news: only working with copies so that lowers the risk. Bad news: a lot of time copying from old media to the local drive.  Also, analysis on the local images would be faster than on the USB mounted drives.  Also, The old NAS devices from Western Digital were amazingly slow and not reachable / mountable on the local file system with NFS so the files had to be SCP’d over.  All those images HAD to be copied locally.   The OS was so old that SSH ciphers offered were deprecated and i had to lower the security of the local SSH client to allow the connections to complete!

Once the analysis process started i was able to remove the Ethernet cable to the machine.  There is nothing it is doing that requires network.  

Once the Approx 150,000 photos from 2004-present were moved over to the local Ubuntu system i ran the analysis command.  After 24 hrs 10,000 images had been processed.  Many of the files touched turned out to be garbage ‘dot’ Files that accompanied the real image file.  I realize i can go through and remove those files even while the processing is ongoing.

**Outstanding**

Of course once i started on the first 150k images i found another batch of photos on another drive.  This poses a problem ( for Claude ) to allow two databases to be integrated into one.  It is a simple issue but another step.

Also there is the issue of a separate maintenance script to find all the images files ‘marked for deletion’ by the human user and then delete them safely.

An upgrade to the presentation layer will come an include more features.  External access from the internet will be nice for the family to have access.

Ultimately, the analysis should be able to identify individual humans so it knows Jake from Sam, and Puffins from Pengiuns, etc.  That is more Model training and development work that will require Sam’s assistance.












